"""Stored strategies and source ownership survive label contract retirement."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy, creator_strategies_equal
from app.schemas.clip_intents import ClipIntent


def test_legacy_voiceover_strategy_round_trips_without_retired_fields():
    raw = {
        "execution_contract": "guided_voiceover_v1",
        "participant_labels": "single_subject",
        "score_labels": True,
        "sport_labels": True,
        "context_label": {"kind": "sport"},
    }
    before = deepcopy(raw)
    parsed = CreativeStrategy.model_validate(raw)
    assert {i.transcript_kind for i in parsed.clip_intents} == {"participant", "score", "topic"}
    assert all(i.label_source == "transcript" for i in parsed.clip_intents)
    serialized = parsed.model_dump(mode="json", exclude_none=True)
    assert (
        not {"participant_labels", "score_labels", "sport_labels", "context_label"}
        & serialized.keys()
    )
    assert raw == before
    assert CreativeStrategy.model_validate(serialized) == parsed
    assert creator_strategies_equal(raw, serialized)
    assert not creator_strategies_equal(raw, {**serialized, "target_duration_s": 30})
    assert not creator_strategies_equal(None, serialized)


def test_mixed_legacy_and_modern_requests_do_not_drop_narration_intent():
    raw = {
        "score_labels": True,
        "clip_intents": [{"intent_id": "dish", "op": "label", "attribute": "the dish shown"}],
    }
    parsed = CreativeStrategy.model_validate(raw)
    assert [(i.intent_id, i.label_source) for i in parsed.clip_intents] == [
        ("dish", "clip"),
        ("legacy-score", "transcript"),
    ]
    assert CreativeStrategy.model_validate(parsed.model_dump()) == parsed


def test_retired_fields_are_not_advertised_in_strategy_schema():
    properties = CreativeStrategy.model_json_schema()["properties"]
    assert (
        not {"participant_labels", "score_labels", "sport_labels", "context_label"}
        & properties.keys()
    )
    with pytest.raises(ValidationError):
        CreativeStrategy.model_validate({"new_per_feature_label": True})


@pytest.mark.parametrize(
    "updates",
    [
        {"label_source": "transcript"},
        {"label_source": "clip", "transcript_kind": "score"},
        {"label_source": "transcript", "transcript_kind": "score", "op": "group"},
        {"label_source": "transcript", "transcript_kind": "score", "creator_text": "9-0"},
        {"label_source": "transcript", "transcript_kind": "arbitrary"},
    ],
)
def test_invalid_source_contracts_fail_closed(updates):
    with pytest.raises(ValidationError):
        ClipIntent.model_validate(
            {"intent_id": "one", "op": "label", "attribute": "score", **updates}
        )


def test_default_clip_source_preserves_old_intent_serialization():
    old = {
        "intent_id": "dish",
        "op": "label",
        "attribute": "dish",
        "creator_text": None,
        "caption_attribute": None,
        "position": None,
    }
    assert ClipIntent.model_validate(old).model_dump() == old


def test_compiler_cannot_promise_transcript_labels_on_a_visual_only_plan():
    from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest

    manifest = resolve_creator_manifest(
        item_id="item",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
        guided_capability_enabled=True,
    )
    strategy = CreativeStrategy(
        render_program="native",
        selected_media_ids=["clip-1"],
        audio_strategy="original_audio",
        clip_intents=[
            ClipIntent(
                intent_id="score",
                op="label",
                attribute="spoken score",
                label_source="transcript",
                transcript_kind="score",
            )
        ],
    )
    with pytest.raises(ValueError, match="recorded guided voiceover"):
        compile_strategy_to_plan(manifest, strategy)
