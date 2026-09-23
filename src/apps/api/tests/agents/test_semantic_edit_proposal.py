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
    _CAPTION_RETRY_HINT,
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
    bindings = [
        {"text": "London day", "chapter_ids": ["one"]},
        {"text": "Park Walk", "chapter_ids": ["one"]},
        {"text": "Wrap", "chapter_ids": ["two"]},
        {"text": "post match pub", "chapter_ids": ["two"]},
    ]
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(text_bindings=bindings),
        _input(shot_labels=["Park Walk", "Pub Talk"], closing_title="Wrap"),
    )
    # Guided stories never render the montage lane: it is dropped wholesale.
    assert plan.text_bindings == []
    assert plan.repairs == [
        "replaced_server_owned_shot_label_thought:0",
        "replaced_server_owned_shot_label_thought:1",
        "dropped_non_montage_text_bindings:4",
    ]
    fast = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(text_bindings=[bindings[0], bindings[3]]),
        _input(direction="fast_montage", closing_title="Wrap"),
    )
    assert "dropped_server_owned_text_binding:0" in fast.repairs
    assert [binding.text for binding in fast.text_bindings] == ["post match pub"] * 2


@pytest.mark.parametrize(
    "direction, repair",
    [
        ("fast_montage", "dropped_server_owned_text_binding:0"),
        ("guided_story", "dropped_non_montage_text_bindings:1"),
    ],
)
def test_normalized_server_title_echo_drops_without_explicit_caption_variant(
    direction: str, repair: str
) -> None:
    raw = _raw(text_bindings=[{"text": "LONDON DAY", "chapter_ids": ["one"]}])
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw, _input(direction=direction, creator_request="")
    )
    assert plan.text_bindings == []
    assert repair in plan.repairs


def test_distinct_caption_variant_of_server_title_is_preserved() -> None:
    raw = _raw(text_bindings=[{"text": "us", "chapter_ids": ["one"]}])
    input = _input(opening_title="US", creator_request='Show "us" on the first clip.')
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    # Guided copy renders through thoughts, so binding-only creator copy moves there.
    assert plan.text_bindings == []
    assert [chapter.thought for chapter in plan.chapters] == ["us", ""]
    assert "moved_creator_caption_binding_to_thought:0:0" in plan.repairs
    fast = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw, input.model_copy(update={"direction": "fast_montage"})
    )
    assert [binding.text for binding in fast.text_bindings] == ["us"]


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
    # Thoughts are the guided lane; the montage bindings are never rendered.
    assert plan.text_bindings == []
    fast = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw), input.model_copy(update={"direction": "fast_montage"})
    )
    assert {binding.text for binding in fast.text_bindings} == {"US", "us"}


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


def test_short_caption_is_not_satisfied_by_another_caption_containing_it() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "USA"
    raw["chapters"][1]["thought"] = ""
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw), _input(creator_request='Show "US" and "USA".')
        )


@pytest.mark.parametrize("direction", ["guided_story", "fast_montage"])
@pytest.mark.parametrize("op", ["caption", "group"])
def test_resolved_caption_echo_cannot_remain_on_unrelated_media(direction, op) -> None:
    intent = ResolvedClipIntent(
        intent_id="caption",
        op=op,
        attribute="pub",
        caption_text="After the match" if op == "caption" else None,
        creator_text="After the match",
        assignments=[ClipAssignment(media_id="pub")],
    )
    raw = json.loads(_raw())
    for chapter in raw["chapters"]:
        chapter["thought"] = "After the match"
    raw["text_bindings"] = [{"text": "After the match", "media_ids": ["park"]}]
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw), _input(direction=direction, creator_request="", clip_intents=[intent])
    )
    assert [chapter.thought for chapter in plan.chapters] == ["", "After the match"]
    assert [
        (binding.text, binding.chapter_ids, binding.media_ids) for binding in plan.text_bindings
    ] == ([("After the match", ["two"], [])] if direction == "fast_montage" else [])


@pytest.mark.parametrize(
    "creator_request",
    [
        'Create a narrated story. He reportedly said: "My client is not in a hurry."',
        'Write a narrated story where he said: "My client is not in a hurry."',
        'He said: "My client is not in a hurry." Put that video on screen.',
        'He said, "My client is not in a hurry."',
        'He said: "My client is not in a hurry." on the video.',
        'Use the words he said: "My client is not in a hurry." for the voiceover.',
        'He said: "My client is not in a hurry." \u2014 use that quote for the voiceover.',
    ],
)
def test_narrated_reported_speech_is_not_required_on_screen(creator_request: str) -> None:
    raw = json.loads(_raw())
    for chapter in raw["chapters"]:
        chapter["thought"] = ""
    input = _input(
        creator_request=creator_request,
        narration_duration_s=24,
    )

    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]

    assert [chapter.thought for chapter in plan.chapters] == ["", ""]
    assert plan.text_bindings == []


@pytest.mark.parametrize(
    "creator_request",
    [
        'Create a narrated story. Put "My client is not in a hurry." on screen.',
        'Create a narrated story. He reportedly said: "My client is not in a hurry." '
        'Put "My client is not in a hurry." on screen.',
        'Show the words he said: "My client is not in a hurry."',
        'Put the words he said: "My client is not in a hurry." on the opening clip.',
        'He reportedly said: "My client is not in a hurry." on screen.',
        'He reportedly said: "My client is not in a hurry." as a caption.',
        'He reportedly said: "My client is not in a hurry." \u2014 put that quote on screen.',
    ],
)
def test_narrated_explicit_display_copy_remains_required(creator_request: str) -> None:
    raw = json.loads(_raw())
    for chapter in raw["chapters"]:
        chapter["thought"] = ""
    input = _input(creator_request=creator_request, narration_duration_s=24)

    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]

    raw["chapters"][0]["thought"] = "My client is not in a hurry."
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert plan.chapters[0].thought == "My client is not in a hurry."


def test_narrated_reported_speech_does_not_replace_explicit_caption() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = ""
    raw["chapters"][1]["thought"] = "After the match"
    input = _input(
        creator_request="Create a narrated story. "
        'He reportedly said: "My client is not in a hurry." '
        'Put "After the match" on screen over the pub clip.',
        narration_duration_s=24,
    )

    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "After the match"]

    raw["chapters"][1]["thought"] = ""
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]


def test_display_instruction_in_previous_clause_does_not_own_reported_speech() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "Intro"
    raw["chapters"][1]["thought"] = ""
    input = _input(
        creator_request='Show "Intro" on screen, then he said: "My client is not in a hurry."',
        narration_duration_s=24,
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["Intro", ""]


@pytest.mark.parametrize("direction", ["guided_story", "text_explainer"])
def test_narrated_story_without_creator_copy_blanks_ai_thoughts(direction: str) -> None:
    """Timed voiceover captions own the body text; AI thoughts would burn over them."""
    raw = _raw(text_bindings=[{"text": "A voiceover line", "chapter_ids": ["one"]}])
    input = _input(direction=direction, creator_request="", narration_duration_s=24)
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", ""]
    assert plan.text_bindings == []
    assert plan.repairs[-3:] == [
        "dropped_non_montage_text_bindings:1",
        "blanked_narrated_thought:0",
        "blanked_narrated_thought:1",
    ]


def test_narration_keeps_shot_labels_resolved_captions_and_unnarrated_drafts() -> None:
    labels = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(), _input(shot_labels=["Park Walk", "Pub Talk"], narration_duration_s=24)
    )
    assert [chapter.thought for chapter in labels.chapters] == ["Park Walk", "Pub Talk"]
    caption = ResolvedClipIntent(
        intent_id="pub",
        op="caption",
        attribute="pub",
        caption_text="After the match",
        assignments=[ClipAssignment(media_id="pub")],
    )
    captioned = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(), _input(creator_request="", clip_intents=[caption], narration_duration_s=24)
    )
    assert [chapter.thought for chapter in captioned.chapters] == ["", "After the match"]
    unnarrated = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(), _input(creator_request="")
    )
    assert [chapter.thought for chapter in unnarrated.chapters] == [
        "A model caption",
        "POST MATCH PUB",
    ]


def test_fast_montage_narration_keeps_thoughts_and_bindings() -> None:
    raw = _raw(text_bindings=[{"text": "post match pub", "chapter_ids": ["two"]}])
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw, _input(direction="fast_montage", narration_duration_s=24)
    )
    assert [binding.text for binding in plan.text_bindings] == ["post match pub"] * 2
    assert not any(
        repair.startswith(("blanked_narrated", "dropped_non_montage")) for repair in plan.repairs
    )


@pytest.mark.parametrize("narration_duration_s", [None, 24])
def test_non_montage_bindings_drop_before_the_twelve_item_cap(
    narration_duration_s: float | None,
) -> None:
    """A narrated story may echo every voiceover sentence as a binding (13 here)."""
    raw = _raw(
        text_bindings=[{"text": f"Voiceover line {i}", "chapter_ids": ["one"]} for i in range(13)]
    )
    input = _input(creator_request="", narration_duration_s=narration_duration_s)
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert "dropped_non_montage_text_bindings:13" in plan.repairs
    with pytest.raises(SchemaError, match="at most 12"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            raw, input.model_copy(update={"direction": "fast_montage"})
        )


def test_moved_caption_uses_media_target_and_never_overwrites_creator_copy() -> None:
    raw = json.loads(_raw(text_bindings=[{"text": "post match pub", "media_ids": ["m002"]}]))
    raw["chapters"][1]["thought"] = ""
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "post match pub"]
    assert "moved_creator_caption_binding_to_thought:0:1" in plan.repairs
    # An invented thought is blanked first, so its chapter can receive the caption.
    raw["chapters"][1]["thought"] = "Invented"
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "post match pub"]
    # The only target already shows other creator copy: never overwrite it. A
    # retry cannot free that shot, so the story renders what fits and says so.
    raw = json.loads(_raw(text_bindings=[{"text": "post match pub", "media_ids": ["m001"]}]))
    raw["chapters"][0]["thought"] = "Park intro"
    raw["chapters"][1]["thought"] = ""
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw), _input(creator_request='Show "Park intro" and "post match pub".')
    )
    assert [chapter.thought for chapter in plan.chapters] == ["Park intro", ""]
    assert "unplaceable_creator_caption:0" in plan.repairs
    assert not any(repair.startswith("moved_creator_caption") for repair in plan.repairs)


def test_narrated_quoted_caption_bound_only_as_binding_still_renders_as_thought() -> None:
    raw = json.loads(
        _raw(
            text_bindings=[
                {"text": "A voiceover line", "chapter_ids": ["one"]},
                {"text": "After the match", "chapter_ids": ["two"]},
            ]
        )
    )
    raw["chapters"][0]["thought"] = "A voiceover line"
    raw["chapters"][1]["thought"] = "Another voiceover line"
    input = _input(
        creator_request='Narrated story. Put "After the match" on screen over the pub clip.',
        narration_duration_s=24,
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "After the match"]
    assert plan.text_bindings == []
    assert "moved_creator_caption_binding_to_thought:1:1" in plan.repairs


def _shared_chapter_raw(**updates: object) -> dict:
    raw = json.loads(_raw(**updates))
    raw["chapters"] = [
        {
            **raw["chapters"][0],
            "thought": "",
            "weight": 3,
            "sources": [{"media_id": "m001", "weight": 1}, {"media_id": "m002", "weight": 2}],
        }
    ]
    return raw


@pytest.mark.parametrize("narration_duration_s", [None, 24])
def test_second_caption_on_a_shared_chapter_splits_its_shot_into_a_chapter(
    narration_duration_s: float | None,
) -> None:
    """Two quoted captions on two shots the model grouped: neither is dropped."""
    raw = _shared_chapter_raw(
        text_bindings=[
            {"text": "Park walk", "media_ids": ["m001"]},
            {"text": "After the match", "media_ids": ["m002"]},
        ]
    )
    input = _input(
        creator_request='Put "Park walk" on screen over the park clip and "After the match" '
        "on screen over the pub clip.",
        narration_duration_s=narration_duration_s,
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [
        (chapter.chapter_id, chapter.role, chapter.thought, [s.media_id for s in chapter.sources])
        for chapter in plan.chapters
    ] == [
        ("one", "hook", "Park walk", ["park"]),
        ("one-part-1", "build", "After the match", ["pub"]),
    ]
    assert [chapter.weight for chapter in plan.chapters] == pytest.approx([1, 2])
    assert "split_creator_caption_source:0:0" in plan.repairs


def _image_input(count: int, **updates: object) -> EditProposalAgentInput:
    media = [
        EditProposalMedia(media_id=f"p{index:02d}", lane="asset", kind="image")
        for index in range(1, count + 1)
    ]
    values: dict[str, object] = {
        "media": media,
        "selected_media_ids": [row.media_id for row in media],
        "target_duration_s": 2.0 * count,
        "opening_title": "",
    }
    values.update(updates)
    return _input(**values)


def _image_chapter(chapter_id: str, first: int, last: int, thought: str = "") -> dict:
    return {
        "chapter_id": chapter_id,
        "topic": "Shots",
        "thought": thought,
        "role": "build",
        "weight": last - first + 1,
        "layout": "fullscreen",
        "sources": [{"media_id": f"m{index:03d}"} for index in range(first, last + 1)],
    }


_THREE_CAPTIONS = {"m001": "Stone arches", "m002": "Blue door", "m003": "Old port"}


@pytest.mark.parametrize(
    "order",
    [
        ["m001", "m002", "m003"],
        ["m002", "m001", "m003"],
        ["m003", "m002", "m001"],
        ["m002", "m003", "m001"],
    ],
)
def test_captions_on_every_shot_of_one_chapter_plan_in_any_binding_order(
    order: list[str],
) -> None:
    """Each caption isolates its own shot, including a middle one, in any order."""
    raw = {
        "title": "x",
        "chapters": [{**_image_chapter("one", 1, 3), "role": "hook"}, _image_chapter("two", 4, 4)],
        "text_bindings": [
            {"text": _THREE_CAPTIONS[alias], "media_ids": [alias]} for alias in order
        ],
    }
    input = _image_input(
        4,
        creator_request='Put "Stone arches" on screen over the first shot, "Blue door" on '
        'screen over the second and "Old port" on screen over the third.',
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [
        (chapter.thought, [s.media_id for s in chapter.sources]) for chapter in plan.chapters
    ] == [
        ("Stone arches", ["p01"]),
        ("Blue door", ["p02"]),
        ("Old port", ["p03"]),
        ("", ["p04"]),
    ]
    assert [chapter.role for chapter in plan.chapters] == ["hook", "build", "build", "build"]
    assert [chapter.weight for chapter in plan.chapters] == pytest.approx([1, 1, 1, 1])
    beats = schedule_semantic_edit(plan, input).story_beats
    assert [beat.thought for beat in beats] == ["Stone arches", "Blue door", "Old port", ""]


def test_caption_bound_to_one_shot_covers_only_that_shot() -> None:
    raw = {
        "title": "x",
        "chapters": [{**_image_chapter("one", 1, 3), "role": "hook"}, _image_chapter("two", 4, 4)],
        "text_bindings": [{"text": "Blue door", "media_ids": ["m002"]}],
    }
    input = _image_input(4, creator_request='Put "Blue door" on screen over the second shot.')
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [
        (chapter.thought, [s.media_id for s in chapter.sources]) for chapter in plan.chapters
    ] == [
        ("", ["p01"]),
        ("Blue door", ["p02"]),
        ("", ["p03"]),
        ("", ["p04"]),
    ]
    assert "split_creator_caption_source:0:1" in plan.repairs


def test_split_chapter_ids_never_collide_with_scheduler_beat_ids() -> None:
    """A >4-source remainder beats as '<id>-2'; a split piece must not take that id."""
    raw = {
        "title": "x",
        "chapters": [{**_image_chapter("one", 1, 7), "role": "hook"}, _image_chapter("two", 8, 8)],
        "text_bindings": [
            {"text": "Harbour lights", "media_ids": ["m004"]},
            {"text": "Last boat", "media_ids": ["m007"]},
            {"text": "First light", "media_ids": ["m001"]},
        ],
    }
    input = _image_input(
        8,
        creator_request='Put "Harbour lights" on screen over the fourth shot, "Last boat" on '
        'screen over the seventh and "First light" on screen over the first.',
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters if chapter.thought] == [
        "First light",
        "Harbour lights",
        "Last boat",
    ]
    beat_ids = [beat.beat_id for beat in schedule_semantic_edit(plan, input).story_beats]
    assert len(beat_ids) == len(set(beat_ids))
    # One caption on the last shot leaves the first piece ("one") with six
    # sources, which beat as "one" and "one-2"; the new piece must take neither.
    raw["text_bindings"] = [{"text": "Last boat", "media_ids": ["m007"]}]
    input = _image_input(8, creator_request='Put "Last boat" on screen over the seventh shot.')
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    beat_ids = [beat.beat_id for beat in schedule_semantic_edit(plan, input).story_beats]
    assert len(beat_ids) == len(set(beat_ids)) == 4


def _twenty_beat_raw(first_thought: str) -> dict:
    """19 chapters and 20 beats: a 4-source chapter, a 5-source one, 17 single shots."""
    chapters = [
        {**_image_chapter("c1", 1, 4, first_thought), "role": "hook"},
        _image_chapter("c2", 5, 9),
        *(_image_chapter(f"c{index - 7}", index, index) for index in range(10, 27)),
    ]
    return {
        "title": "x",
        "chapters": chapters,
        "text_bindings": [{"text": "Fresh figs", "media_ids": ["m004"]}],
    }


def test_split_that_would_pass_the_beat_limit_keeps_the_whole_chapter() -> None:
    input = _image_input(26, creator_request='Put "Fresh figs" on screen over the fig photo.')
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(_twenty_beat_raw("")), input
    )
    assert len(plan.chapters) == 19
    assert plan.chapters[0].thought == "Fresh figs"
    assert not any(repair.startswith("split_creator_caption_source") for repair in plan.repairs)
    assert len(schedule_semantic_edit(plan, input).story_beats) == 20
    # With the chapter already captioned and the split over the beat limit,
    # there is nowhere to draw it: never schedule a 21st beat or overwrite
    # "Market day"; render what fits and record the unplaceable caption.
    agent = SemanticEditProposalAgent(None)
    captioned_input = input.model_copy(
        update={
            "creator_request": 'Put "Market day" on screen over the stall photos and '
            '"Fresh figs" on screen over the fig photo.'
        }
    )
    plan = agent.parse(  # type: ignore[arg-type]
        json.dumps(_twenty_beat_raw("Market day")), captioned_input
    )
    assert len(plan.chapters) == 19
    assert [chapter.thought for chapter in plan.chapters if chapter.thought] == ["Market day"]
    assert "unplaceable_creator_caption:0" in plan.repairs
    assert agent._schema_retry_hint is None
    assert len(schedule_semantic_edit(plan, captioned_input).story_beats) == 20


_THREE_QUOTED_CAPTIONS = 'Show "Park intro", "post match pub" and "Cheers".'


@pytest.mark.parametrize(
    "direction, bindings",
    [
        *(
            pytest.param(direction, bindings, id=f"{direction}-{case}")
            for direction in ("guided_story", "text_explainer")
            for case, bindings in (
                ("never_bound", []),
                ("other_text", [{"text": "invented", "chapter_ids": ["one"]}]),
                (
                    "no_real_target",
                    [
                        {"text": "post match pub", "chapter_ids": ["missing"]},
                        {"text": "Cheers", "media_ids": ["m000"]},
                    ],
                ),
                # One caption is proven unplaceable; the omitted one still fails.
                ("beside_unplaceable", [{"text": "post match pub", "media_ids": ["m001"]}]),
            )
        ),
        pytest.param("fast_montage", [], id="fast_montage-never_bound"),
    ],
)
def test_caption_the_model_never_placed_fails_with_a_server_retry_hint(
    direction: str, bindings: list[dict]
) -> None:
    """Only a binding to real, fully taken chapters excuses a caption."""
    raw = json.loads(_raw(text_bindings=bindings))
    raw["chapters"][0]["thought"] = "Park intro"
    raw["chapters"][1]["thought"] = ""
    agent = SemanticEditProposalAgent(None)
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        agent.parse(  # type: ignore[arg-type]
            json.dumps(raw),
            _input(direction=direction, creator_request=_THREE_QUOTED_CAPTIONS),
        )
    assert agent._schema_retry_hint == _CAPTION_RETRY_HINT


@pytest.mark.parametrize("direction", ["guided_story", "text_explainer"])
def test_moved_caption_never_targets_a_resolved_group_chapter(direction: str) -> None:
    """The group intent owns that chapter's thought; it would overwrite the caption."""
    group = ResolvedClipIntent(
        intent_id="pub",
        op="group",
        attribute="pub",
        creator_text="post match pub",
        assignments=[ClipAssignment(media_id="pub")],
    )
    raw = json.loads(_raw(text_bindings=[{"text": "Cheers", "media_ids": ["m002"]}]))
    raw["chapters"][1]["thought"] = ""
    input = _input(
        direction=direction,
        creator_request='Put "Cheers" on screen over the pub clip.',
        clip_intents=[group],
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    # The group keeps its chapter; the caption never moves to the free park chapter.
    assert [chapter.thought for chapter in plan.chapters] == ["", "post match pub"]
    assert "unplaceable_creator_caption:0" in plan.repairs
    assert not any(repair.startswith("moved_creator_caption") for repair in plan.repairs)


_SPORTS_DAY_IDS = ["clip_park", "clip_soccer", "clip_volleyball", "clip_speech", "clip_pub"]


def _sports_day_input(direction: str, caption_request: str) -> EditProposalAgentInput:
    """The X2/X4 differential shapes: five single-shot clips under once reuse."""
    return _input(
        direction=direction,
        creator_request="Group the pub videos together and call that chapter 'post match "
        "pub', order the park clips first and include the part where I talk to the camera. "
        + caption_request,
        opening_title="Park then pub",
        target_duration_s=25,
        video_reuse_policy="once",
        selected_media_ids=_SPORTS_DAY_IDS,
        media=[
            EditProposalMedia(media_id=media_id, lane="clip", kind="video", duration_s=5.0)
            for media_id in _SPORTS_DAY_IDS
        ],
        clip_intents=[
            ResolvedClipIntent(
                intent_id="park-first",
                op="order",
                attribute="park clips",
                position="first",
                assignments=[ClipAssignment(media_id="clip_park")],
            ),
            ResolvedClipIntent(
                intent_id="pub-group",
                op="group",
                attribute="pub videos",
                creator_text="post match pub",
                assignments=[ClipAssignment(media_id="clip_pub")],
            ),
            ResolvedClipIntent(
                intent_id="speech",
                op="include",
                attribute="where I talk to the camera",
                assignments=[ClipAssignment(media_id="clip_speech")],
            ),
        ],
    )


def _sports_day_raw(speech_thought: str, bindings: list[dict]) -> str:
    topics = ["Park", "Soccer", "Volleyball", "Talking", "Pub"]
    thoughts = ["", "", "", speech_thought, "post match pub"]
    return json.dumps(
        {
            "title": "Park then pub",
            "chapters": [
                {
                    "chapter_id": f"chapter-{index + 1}",
                    "topic": topic,
                    "thought": thought,
                    "role": "hook" if index == 0 else "payoff" if index == 4 else "build",
                    "weight": 0.2,
                    "layout": "fullscreen",
                    "sources": [{"media_id": f"m{index + 1:03d}", "weight": 1.0}],
                }
                for index, (topic, thought) in enumerate(zip(topics, thoughts, strict=True))
            ],
            "text_bindings": bindings,
        }
    )


@pytest.mark.parametrize("direction", ["guided_story", "text_explainer"])
def test_second_caption_for_a_captioned_single_shot_renders_what_fits(direction: str) -> None:
    """X2: two quoted captions for one clip under once reuse; a retry cannot fit both."""
    input = _sports_day_input(
        direction, 'Put "Hello" and "Goodbye" on screen over the talking clip.'
    )
    raw = _sports_day_raw("Hello", [{"text": "Goodbye", "media_ids": ["m004"]}])
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == [
        "",
        "",
        "",
        "Hello",
        "post match pub",
    ]
    assert "unplaceable_creator_caption:0" in plan.repairs
    beats = schedule_semantic_edit(plan, input).story_beats
    assert [beat.thought for beat in beats if beat.thought] == ["Hello", "post match pub"]


@pytest.mark.parametrize("direction", ["guided_story", "text_explainer"])
def test_quoted_caption_on_a_resolved_group_member_renders_what_fits(direction: str) -> None:
    """X4: the group owns its chapter's thought; the caption never lands elsewhere."""
    input = _sports_day_input(direction, 'Put "Cheers" on screen over the pub clip.')
    raw = _sports_day_raw("", [{"text": "Cheers", "media_ids": ["m005"]}])
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "", "", "", "post match pub"]
    assert "unplaceable_creator_caption:0" in plan.repairs
    beats = schedule_semantic_edit(plan, input).story_beats
    assert [beat.thought for beat in beats if beat.thought] == ["post match pub"]


def test_narrated_second_caption_on_a_captioned_photo_renders_what_fits() -> None:
    """N3: a narrated story quotes two captions for one photo under once reuse."""
    input = _input(
        creator_request='Narrated story. Put "1882" and "Keeper" on screen over the old portrait.',
        narration_duration_s=24,
        video_reuse_policy="once",
        opening_title="One light on the coast",
        selected_media_ids=["park", "pub", "portrait"],
        media=[
            *(media.model_copy(update={"duration_s": 5.0}) for media in _input().media),
            EditProposalMedia(
                media_id="portrait", lane="asset", kind="image", summary="an old portrait"
            ),
        ],
    )
    raw = json.loads(_raw(text_bindings=[{"text": "Keeper", "media_ids": ["m003"]}]))
    for chapter in raw["chapters"]:
        chapter["sources"][0].pop("candidate_index")
    raw["chapters"][0]["thought"] = "Every ship looked for one light."
    raw["chapters"][1]["thought"] = "She kept watch for forty winters."
    raw["chapters"].insert(
        1,
        {
            "chapter_id": "portrait",
            "topic": "The first keeper",
            "thought": "1882",
            "role": "build",
            "weight": 1,
            "layout": "fullscreen",
            "sources": [{"media_id": "m003", "weight": 1}],
        },
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    # Voiceover lines are blanked; the portrait keeps "1882" and "Keeper" is recorded.
    assert [chapter.thought for chapter in plan.chapters] == ["", "1882", ""]
    assert "unplaceable_creator_caption:0" in plan.repairs
    assert {"blanked_narrated_thought:0", "blanked_narrated_thought:2"} <= set(plan.repairs)
    beats = schedule_semantic_edit(plan, input).story_beats
    assert [beat.thought for beat in beats if beat.thought] == ["1882"]


@pytest.mark.parametrize(
    "extra",
    [
        {"thought": "The last voiceover line", "sources": []},
        {"thought": "The last voiceover line", "sources": [{"media_id": "m000"}]},
    ],
    ids=["sourceless", "invented_alias"],
)
def test_narrated_extra_line_chapter_is_dropped_not_fatal(extra: dict) -> None:
    """Narrated thoughts never render, so they cannot keep a groundless chapter."""
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "x" * 400  # over the 280-char schema cap
    raw["chapters"].append({"chapter_id": "three", "topic": "End", "role": "payoff", **extra})
    input = _input(creator_request="", narration_duration_s=24)
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert [chapter.thought for chapter in plan.chapters] == ["", ""]
    assert {"blanked_narrated_thought:0", "blanked_narrated_thought:1"} <= set(plan.repairs)
    with pytest.raises(SchemaError):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw), input.model_copy(update={"narration_duration_s": None})
        )


@pytest.mark.parametrize(
    "creator_request, required",
    [
        ('Narrated. "The sea keeps its own hours," she reportedly said.', False),
        ('Narrated. Locals call it "the sea keeps its own hours".', False),
        ('Narrated. "The sea keeps its own hours" on screen over the pub clip.', True),
        ('Narrated. Caption the pub clip "The sea keeps its own hours".', True),
    ],
)
def test_quote_spoken_in_the_voiceover_is_script_not_required_copy(
    creator_request: str, required: bool
) -> None:
    words = "then she said the sea keeps its own hours and left".split()
    narration_words = [
        {"text": word, "start_s": index * 0.5, "end_s": index * 0.5 + 0.4}
        for index, word in enumerate(words)
    ]
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = ""
    raw["chapters"][1]["thought"] = "The sea keeps its own hours"
    input = _input(
        creator_request=creator_request,
        narration_duration_s=24,
        narration_words=narration_words,
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    expected = "The sea keeps its own hours" if required else ""
    assert [chapter.thought for chapter in plan.chapters] == ["", expected]
    raw["chapters"][1]["thought"] = ""
    if required:
        with pytest.raises(SchemaError, match="creator caption was dropped"):
            SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    else:
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]


_SEA_LINE = "The sea keeps its own hours"


@pytest.mark.parametrize(
    "creator_request, thoughts",
    [
        (f'Narrated. Add "{_SEA_LINE}" to the pub clip.', ["", _SEA_LINE]),
        (f'Narrated. Write "{_SEA_LINE}".', ["", _SEA_LINE]),
        (f'Narrated. "{_SEA_LINE}" over the pub photo.', ["", _SEA_LINE]),
        (f'Narrated. "{_SEA_LINE}" as text.', ["", _SEA_LINE]),
        ('Narrated. Locals call it "own hours".', ["", "own hours"]),
        (f'Narrated. Label them "Last orders" and "{_SEA_LINE}".', ["Last orders", _SEA_LINE]),
        (f'Narrated. "{_SEA_LINE}" and "Last orders" on screen.', ["Last orders", _SEA_LINE]),
    ],
    ids=["add", "write", "over_photo", "as_text", "short_label", "list_cue", "list_suffix"],
)
def test_spoken_quote_with_explicit_placement_stays_creator_copy(
    creator_request: str, thoughts: list[str]
) -> None:
    """Explicit on-screen copy the voiceover also speaks must still render."""
    words = "then she said the sea keeps its own hours and left".split()
    narration_words = [
        {"text": word, "start_s": index * 0.5, "end_s": index * 0.5 + 0.4}
        for index, word in enumerate(words)
    ]
    input = _input(
        creator_request=creator_request,
        narration_duration_s=24,
        narration_words=narration_words,
    )
    raw = json.loads(_raw())
    for chapter, thought in zip(raw["chapters"], thoughts, strict=True):
        chapter["thought"] = thought
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == thoughts
    raw["chapters"][1]["thought"] = ""
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]


_PORTRAIT_CAPTION = ResolvedClipIntent(
    intent_id="portrait",
    op="caption",
    attribute="the pub clip",
    creator_text="1882",
    caption_text="1882",
    assignments=[ClipAssignment(media_id="pub")],
)


@pytest.mark.parametrize("copy", ["quoted", "resolved_intent"])
@pytest.mark.parametrize(
    "extra",
    [
        {"thought": "The last voiceover line", "sources": []},
        {"thought": "The last voiceover line", "sources": [{"media_id": "m000"}]},
    ],
    ids=["sourceless", "invented_alias"],
)
def test_narrated_draft_thoughts_blank_before_validation_beside_creator_copy(
    extra: dict, copy: str
) -> None:
    """One creator caption must not bring back the narrated prod failure shapes."""
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "x" * 400  # over the 280-char schema cap
    raw["chapters"][1]["thought"] = "1882"
    raw["chapters"].append({"chapter_id": "three", "topic": "End", "role": "payoff", **extra})
    input = _input(
        creator_request='Put "1882" on screen over the pub clip.',
        narration_duration_s=24,
        clip_intents=[_PORTRAIT_CAPTION] if copy == "resolved_intent" else None,
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert [chapter.thought for chapter in plan.chapters] == ["", "1882"]
    assert "blanked_narrated_thought:0" in plan.repairs


def test_repeated_source_in_one_chapter_merges_into_one_longer_window() -> None:
    raw = json.loads(_raw())
    raw["chapters"][1]["sources"] = [
        {"media_id": "m002", "weight": 1},
        {"media_id": "m002", "weight": 2},
    ]
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]
    assert [(s.media_id, s.weight) for s in plan.chapters[1].sources] == [("pub", 3)]
    assert "merged_repeated_source:1:1" in plan.repairs


def test_non_list_bindings_only_fail_the_montage_that_renders_them() -> None:
    raw = _raw(text_bindings={"text": "A voiceover line"})
    plan = SemanticEditProposalAgent(None).parse(raw, _input())  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert "dropped_invalid_non_montage_text_bindings" in plan.repairs
    with pytest.raises(SchemaError, match="text_bindings must be a list"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            raw, _input(direction="fast_montage")
        )


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


@pytest.mark.parametrize("pub_op", ["caption", "group"])
def test_resolved_caption_intents_override_only_assigned_guided_chapters(pub_op) -> None:
    intents = [
        ResolvedClipIntent(
            intent_id="creator-caption",
            op="caption",
            attribute="park",
            creator_text="Park intro",
            caption_text="Park intro",
            assignments=[ClipAssignment(media_id="park")],
        ),
        ResolvedClipIntent(
            intent_id="grounded-caption",
            op=pub_op,
            attribute="pub",
            caption_text="After the match" if pub_op == "caption" else None,
            creator_text="After the match" if pub_op == "group" else None,
            assignments=[ClipAssignment(media_id="pub")],
        ),
    ]
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "Wrong model copy"
    raw["chapters"][1]["thought"] = "Also wrong"
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(creator_request="", clip_intents=intents),
    )
    assert [chapter.thought for chapter in plan.chapters] == ["Park intro", "After the match"]


def test_resolved_caption_intents_fast_bind_only_their_assigned_chapters() -> None:
    intent = ResolvedClipIntent(
        intent_id="caption",
        op="caption",
        attribute="pub",
        caption_text="After the match",
        assignments=[ClipAssignment(media_id="pub")],
    )
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _raw(), _input(direction="fast_montage", creator_request="", clip_intents=[intent])
    )
    assert [(binding.text, binding.chapter_ids) for binding in plan.text_bindings] == [
        ("After the match", ["two"])
    ]


def test_resolved_caption_intents_reject_conflicting_or_missing_members() -> None:
    conflict = [
        ResolvedClipIntent(
            intent_id="one",
            op="caption",
            attribute="park",
            caption_text="One",
            assignments=[ClipAssignment(media_id="park")],
        ),
        ResolvedClipIntent(
            intent_id="two",
            op="caption",
            attribute="park",
            caption_text="Two",
            assignments=[ClipAssignment(media_id="park")],
        ),
    ]
    with pytest.raises(SchemaError, match="caption intents conflict"):
        SemanticEditProposalAgent(None).parse(
            _raw(), _input(creator_request="", clip_intents=conflict)
        )  # type: ignore[arg-type]
    missing = [
        ResolvedClipIntent(
            intent_id="missing",
            op="caption",
            attribute="missing",
            caption_text="Missing",
            assignments=[ClipAssignment(media_id="missing")],
        )
    ]
    with pytest.raises(SchemaError, match="requested media coverage was dropped"):
        SemanticEditProposalAgent(None).parse(
            _raw(), _input(creator_request="", clip_intents=missing)
        )  # type: ignore[arg-type]


_RUNNING_LABEL = ResolvedClipIntent(
    intent_id="sport",
    op="label",
    attribute="sport",
    assignments=[ClipAssignment(media_id="park", value="Running")],
)


@pytest.mark.parametrize(
    "direction, repair",
    [
        ("fast_montage", "dropped_grounded_label_text_binding:0"),
        ("guided_story", "dropped_non_montage_text_bindings:1"),
    ],
)
def test_resolved_label_generic_text_binding_is_dropped_for_grounded_lane(
    direction: str, repair: str
) -> None:
    input = _input(direction=direction, creator_request="", clip_intents=[_RUNNING_LABEL])
    raw = json.loads(_raw(text_bindings=[{"text": "Running", "media_ids": ["m001"]}]))
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert repair in plan.repairs


def test_binding_only_label_copy_is_not_moved_into_a_guided_thought() -> None:
    """The grounded label lane already draws it; a thought would draw it twice."""
    input = _input(
        creator_request='Show "Running" on the park clip.', clip_intents=[_RUNNING_LABEL]
    )
    raw = _raw(text_bindings=[{"text": "Running", "media_ids": ["m001"]}])
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", ""]
    assert not any(repair.startswith("moved_creator_caption") for repair in plan.repairs)


@pytest.mark.parametrize(
    "direction, repair",
    [
        ("fast_montage", "dropped_unrequested_text_binding:0"),
        ("guided_story", "dropped_non_montage_text_bindings:1"),
    ],
)
def test_unrequested_generic_text_binding_is_dropped_when_captions_are_explicit(
    direction: str, repair: str
) -> None:
    raw = _raw(text_bindings=[{"text": "invented", "chapter_ids": ["one"]}])
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw, _input(direction=direction)
    )
    assert repair in plan.repairs
    # fast_montage re-binds the allowlisted thought; guided keeps it as a thought.
    assert [binding.text for binding in plan.text_bindings] == (
        ["post match pub"] if direction == "fast_montage" else []
    )


def test_unrequested_binding_with_unknown_target_still_rejects() -> None:
    raw = _raw(text_bindings=[{"text": "invented", "chapter_ids": ["missing"]}])
    with pytest.raises(SchemaError, match="references unknown target"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            raw, _input(direction="fast_montage")
        )
    # A guided story never renders the montage lane, so its targets are not validated.
    plan = SemanticEditProposalAgent(None).parse(raw, _input())  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert "dropped_non_montage_text_bindings:1" in plan.repairs


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


@pytest.mark.parametrize("op", ["group", "caption"])
def test_semantic_prompt_marks_only_resolved_group_members_per_media(op) -> None:
    input = _input(
        clip_intents=[
            ResolvedClipIntent(
                intent_id="park-only",
                op=op,
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
            json.dumps(bad),
            _input(creator_request="", clip_intents=intents, video_reuse_policy="allow_repeat"),
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
            json.dumps(raw),
            _input(creator_request="", clip_intents=[group], video_reuse_policy="allow_repeat"),
        )


class _QueuedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def invoke(self, **kwargs: object) -> ModelInvocation:
        self.prompts.append(str(kwargs["prompt"]))
        return ModelInvocation(raw_text=self.responses.pop(0))


@pytest.mark.parametrize("op", ["group", "caption"])
def test_group_schema_retry_names_exclusive_aliases_without_model_output(op) -> None:
    group = ResolvedClipIntent(
        intent_id="park-only",
        op=op,
        attribute="park together",
        caption_text="Park" if op == "caption" else None,
        assignments=[ClipAssignment(media_id="park")],
    )
    invalid = json.loads(_raw())
    invalid["chapters"][0]["topic"] = "MODEL_ONLY_POLLUTION"
    invalid["chapters"][0]["sources"] = [{"media_id": "m001"}, {"media_id": "m002"}]
    client = _QueuedClient([json.dumps(invalid), _raw()])
    agent = SemanticEditProposalAgent(client)  # type: ignore[arg-type]
    agent.spec = replace(agent.spec, model="test-model", max_attempts=2)

    plan = agent.run(
        _input(creator_request="", clip_intents=[group], video_reuse_policy="allow_repeat")
    )

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


def test_label_count_retry_includes_exact_count_without_echoing_model_output() -> None:
    agent = SemanticEditProposalAgent(None)  # type: ignore[arg-type]
    raw = json.loads(_raw())
    raw["chapters"].append({**raw["chapters"][0], "chapter_id": "MODEL_ONLY_POLLUTION"})
    with pytest.raises(SchemaError, match="one chapter each"):
        agent.parse(json.dumps(raw), _input(shot_labels=["First", "Second"]))
    hint = agent.schema_clarification()
    assert "Return exactly 2 chapters" in hint
    assert "each video alias can appear only once" in hint
    assert "MODEL_ONLY_POLLUTION" not in hint


def test_surplus_unknown_alias_chapter_is_dropped_and_shifted_aliases_retry_with_range() -> None:
    raw = json.loads(_raw())
    raw["chapters"].append(
        {
            **raw["chapters"][1],
            "chapter_id": "ghost",
            "thought": "",
            "sources": [{"media_id": "m000"}],
        }
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]
    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert plan.repairs[:2] == [
        "dropped_unknown_media_chapter:2",
        "dropped_unknown_media_source:2:0",
    ]
    # An invented alias standing in for a real source is not surplus: never guess.
    raw = json.loads(_raw())
    raw["chapters"][1]["sources"] = [{"media_id": "m000", "candidate_index": 0}]
    agent = SemanticEditProposalAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError, match="source references unknown media"):
        agent.parse(json.dumps(raw), _input())
    hint = agent.schema_clarification()
    assert "m001 through m002" in hint
    assert "there is no m000" in hint
    assert "POST MATCH PUB" not in hint


def test_shifted_alias_retry_prompt_lists_the_valid_alias_range() -> None:
    invalid = json.loads(_raw())
    invalid["chapters"][1]["topic"] = "MODEL_ONLY_POLLUTION"
    invalid["chapters"][1]["sources"] = [{"media_id": "m000"}]
    client = _QueuedClient([json.dumps(invalid), _raw()])
    agent = SemanticEditProposalAgent(client)  # type: ignore[arg-type]
    agent.spec = replace(agent.spec, model="test-model", max_attempts=2)

    plan = agent.run(_input())

    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert len(client.prompts) == 2
    assert "there is no m000" not in client.prompts[0]
    assert "valid media aliases are m001 through m002" in client.prompts[1]
    assert "MODEL_ONLY_POLLUTION" not in client.prompts[1]


def test_unknown_alias_chapter_with_copy_still_rejects() -> None:
    raw = json.loads(_raw())
    raw["chapters"].append(
        {
            **raw["chapters"][1],
            "chapter_id": "ghost",
            "thought": "Invented",
            "sources": [{"media_id": "m000"}],
        }
    )
    with pytest.raises(SchemaError, match="unknown media"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]


def _misassigned_group_case():
    path = (
        Path(__file__).parents[1]
        / "fixtures/agent_evals/semantic_edit_proposal/golden/kri129_pub_member_reconciliation.json"
    )
    fixture = json.loads(path.read_text())
    return json.loads(fixture["raw_text"]), EditProposalAgentInput.model_validate(fixture["input"])


def test_resolved_group_recovery_preserves_every_source_priority_and_candidate() -> None:
    raw, input = _misassigned_group_case()
    raw["chapters"][1]["sources"][0]["candidate_index"] = 0
    input.media[6].best_moments = [{"start_s": 0, "end_s": 1}]
    aliases = {f"m{i + 1:03d}": media.media_id for i, media in enumerate(input.media)}
    priorities = {}
    candidates = {}
    for chapter in raw["chapters"]:
        total = sum(source.get("weight", 1) for source in chapter["sources"])
        for source in chapter["sources"]:
            media_id = aliases[source["media_id"]]
            priorities[media_id] = chapter.get("weight", 1) * source.get("weight", 1) / total
            candidates[media_id] = source.get("candidate_index")
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    actual = {}
    for chapter in plan.chapters:
        total = sum(source.weight for source in chapter.sources)
        for source in chapter.sources:
            assert source.media_id not in actual
            actual[source.media_id] = chapter.weight * source.weight / total
            assert source.candidate_index == candidates[source.media_id]
    assert actual == pytest.approx(priorities)
    assert plan.montage_audio == input.montage_audio
    assert any(repair.startswith("recovered_misassigned_group:") for repair in plan.repairs)
    scheduled = schedule_semantic_edit(plan, input)
    assert scheduled.schedule.total_frames == 1800
    expected = next(intent.media_ids() for intent in input.clip_intents if intent.op == "group")
    assert [source.media_id for source in plan.chapters[-1].sources] == expected


@pytest.mark.parametrize(
    "unsafe",
    [
        "duplicate",
        "repeat_duplicate",
        "photo_duplicate",
        "ambiguous",
        "overlap",
        "order",
        "binding",
        "anchor_binding",
        "layout",
        "shot_labels",
        "overflow",
    ],
)
def test_resolved_group_recovery_rejects_unsafe_moves(unsafe) -> None:
    raw, input = _misassigned_group_case()
    stray = raw["chapters"][1]["sources"][0]
    stray_id = input.media[int(stray["media_id"][1:]) - 1].media_id
    if unsafe in {"duplicate", "repeat_duplicate", "photo_duplicate"}:
        raw["chapters"][-1]["sources"].append(dict(stray))
        if unsafe == "repeat_duplicate":
            input.video_reuse_policy = "allow_repeat"
        elif unsafe == "photo_duplicate":
            input.media[6].kind = "image"
    elif unsafe == "ambiguous":
        raw["chapters"].insert(
            3,
            {
                **raw["chapters"][-1],
                "chapter_id": "second-exclusive-anchor",
                "sources": [raw["chapters"][-1]["sources"].pop()],
            },
        )
    elif unsafe == "overlap":
        input.clip_intents.append(
            ResolvedClipIntent(
                intent_id="overlap",
                op="group",
                attribute="another group",
                creator_text="Another caption",
                assignments=[ClipAssignment(media_id=stray_id)],
            )
        )
    elif unsafe == "order":
        input.clip_intents.append(
            ResolvedClipIntent(
                intent_id="stray-first",
                op="order",
                position="first",
                attribute="stray first",
                assignments=[ClipAssignment(media_id=stray_id)],
            )
        )
    elif unsafe in {"binding", "anchor_binding"}:
        input.creator_request += ' Also show "Speech" on the speaking chapter.'
        raw["text_bindings"] = [
            {
                "text": "Speech",
                "chapter_ids": [raw["chapters"][1 if unsafe == "binding" else -1]["chapter_id"]],
            }
        ]
    elif unsafe == "layout":
        raw["chapters"][1]["layout"] = "supporting_card"
    elif unsafe == "overflow":
        raw["chapters"][1]["weight"] = 1.7e308
        raw["chapters"][-1]["weight"] = 1.7e308
    else:
        input.shot_labels = [f"Label {i}" for i in range(len(raw["chapters"]))]
    with pytest.raises(SchemaError):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]


@pytest.mark.parametrize("bound_chapter", [1, -1])
def test_guided_group_caption_binding_does_not_block_group_recovery(bound_chapter: int) -> None:
    """Server-placed group copy is not moved from a dropped binding into a thought."""
    raw, input = _misassigned_group_case()
    input.direction = "guided_story"
    group = next(intent for intent in input.clip_intents if intent.op == "group")
    raw["chapters"][-1]["thought"] = ""
    raw["text_bindings"] = [
        {"text": group.creator_text, "chapter_ids": [raw["chapters"][bound_chapter]["chapter_id"]]}
    ]
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert any(repair.startswith("recovered_misassigned_group:") for repair in plan.repairs)
    assert not any(repair.startswith("moved_creator_caption") for repair in plan.repairs)
    assert plan.chapters[-1].thought == group.creator_text
    assert plan.text_bindings == []


@pytest.mark.parametrize("direction", ["guided_story", "text_explainer"])
def test_caption_beside_a_misassigned_group_member_lands_after_recovery(direction: str) -> None:
    """Recovery first moves the stray pub clip out; its old chapter can then take copy."""
    raw, input = _misassigned_group_case()
    input.direction = direction
    input.creator_request += ' Put "Warm up" on screen over the eleventh clip.'
    raw["text_bindings"] = [{"text": "Warm up", "media_ids": ["m011"]}]
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert any(repair.startswith("recovered_misassigned_group:") for repair in plan.repairs)
    captioned = next(chapter for chapter in plan.chapters if chapter.thought == "Warm up")
    assert [source.media_id for source in captioned.sources] == [input.media[10].media_id]
    group = next(intent for intent in input.clip_intents if intent.op == "group")
    assert [source.media_id for source in plan.chapters[-1].sources] == group.media_ids()
    assert plan.chapters[-1].thought == group.creator_text


def test_valid_contiguous_group_chapters_are_not_merged_by_recovery() -> None:
    raw, input = _misassigned_group_case()
    stray = raw["chapters"][1]["sources"].pop(0)
    raw["chapters"].insert(
        -1, {**raw["chapters"][-1], "chapter_id": "pub-part-one", "sources": [stray]}
    )
    expected = [(chapter["chapter_id"], len(chapter["sources"])) for chapter in raw["chapters"]]
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [(chapter.chapter_id, len(chapter.sources)) for chapter in plan.chapters] == expected
    assert not any(repair.startswith("recovered_misassigned_group:") for repair in plan.repairs)


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
