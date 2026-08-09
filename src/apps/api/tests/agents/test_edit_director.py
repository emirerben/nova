from __future__ import annotations

import json
import uuid
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.agents._runtime import ModelClient, ModelInvocation, RunContext
from app.agents.edit_director import (
    EditDirectorAgent,
    EditDirectorFallbackAgent,
    EditDirectorInput,
)
from app.routes import _director


def _snapshot() -> dict:
    return {
        "allowed_op_families": [
            "text",
            "timeline",
            "effect",
            "transition",
            "visual",
            "title",
        ],
        "total_duration_s": 9.0,
        "text_bars": [{"text": "old hook", "start_s": 0.0, "end_s": 2.5}],
        "slots": [
            {
                "key": "slot-0",
                "clip_index": 0,
                "in_s": 0.0,
                "output_start_s": 0.0,
                "output_end_s": 3.0,
                "duration_s": 3.0,
            },
            {
                "key": "slot-1",
                "clip_index": 1,
                "in_s": 0.0,
                "output_start_s": 3.0,
                "output_end_s": 6.0,
                "duration_s": 3.0,
            },
            {
                "key": "slot-2",
                "clip_index": 2,
                "in_s": 0.0,
                "output_start_s": 6.0,
                "output_end_s": 9.0,
                "duration_s": 3.0,
            },
        ],
        "camera_effects": [{"start_s": 0.4, "end_s": 1.5, "intensity": 0.04}],
        "visual_blocks": [
            {
                "id": "visual-1",
                "kind": "montage",
                "start_s": 3.0,
                "end_s": 6.0,
                "transition_in": "cut",
                "transition_out": "cut",
            }
        ],
    }


def _suggestion(category: str, title: str, op: dict) -> dict:
    return {
        "category": category,
        "title": title,
        "rationale": "This resolves a specific editorial weakness in the current cut.",
        "expected_benefit": "A clearer, more intentional viewing rhythm.",
        "confidence": 0.86,
        "start_s": 0.0,
        "end_s": 3.0,
        "apply_mode": "instant",
        "ops": [op],
    }


def _valid_suggestions() -> list[dict]:
    return [
        _suggestion(
            "text",
            "Sharpen the opening promise",
            {"op": "edit_text", "bar_index": 0, "text": "Wait for the last detail"},
        ),
        _suggestion(
            "hook_pacing",
            "Tighten the second beat",
            {"op": "set_clip_duration", "slot_index": 1, "duration_s": 2.2},
        ),
        _suggestion(
            "effect",
            "Add a restrained hook pulse",
            {"op": "add_camera_effect", "start_s": 0.4, "end_s": 1.4, "intensity": 1},
        ),
        _suggestion(
            "text",
            "Clarify the working title",
            {"op": "set_title", "title": "A clearer working title"},
        ),
    ]


def _parse(suggestions: list[dict], **input_overrides):
    return EditDirectorAgent(ModelClient()).parse(
        json.dumps({"suggestions": suggestions}),
        EditDirectorInput(variant_snapshot=_snapshot(), **input_overrides),
    )


class _RawResponseClient(ModelClient):
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text

    def invoke(self, **kwargs) -> ModelInvocation:  # noqa: ARG002
        return ModelInvocation(raw_text=self.raw_text)


def test_director_returns_ranked_validated_bundles() -> None:
    output = _parse(_valid_suggestions())

    assert len(output.suggestions) == 4
    assert output.suggestions[2].ops[0]["intensity"] == 0.08
    assert output.suggestions[3].ops[0]["title"] == "A clearer working title"
    assert all(item.id.startswith("director-") for item in output.suggestions)


def test_director_clamps_transition_duration() -> None:
    transition = _suggestion(
        "transition",
        "Flash into the reveal",
        {
            "op": "set_transition",
            "boundary_index": 0,
            "transition": "flash",
            "duration_s": 2,
        },
    )

    output = _parse([transition])

    assert output.suggestions[0].ops[0]["duration_s"] == 0.3


def test_director_preserves_two_valid_suggestions_instead_of_failing_the_review() -> None:
    """Production models often return two useful cards after validation."""
    output = _parse(_valid_suggestions()[:2])

    assert [item.title for item in output.suggestions] == [
        "Sharpen the opening promise",
        "Tighten the second beat",
    ]


def test_director_preserves_one_valid_suggestion() -> None:
    output = _parse(_valid_suggestions()[:1])

    assert [item.title for item in output.suggestions] == ["Sharpen the opening promise"]


def test_director_preserves_five_valid_suggestions() -> None:
    fifth = _suggestion(
        "text",
        "Add a closing callout",
        {"op": "add_text", "text": "Save this for later", "start_s": 7.0, "end_s": 8.5},
    )

    output = _parse([*_valid_suggestions(), fifth])

    assert len(output.suggestions) == 5
    assert output.suggestions[-1].title == "Add a closing callout"


def test_director_repairs_truncated_json_before_validation() -> None:
    raw_text = json.dumps({"suggestions": _valid_suggestions()[:3]})[:-2]

    output = EditDirectorAgent(_RawResponseClient(raw_text)).run(
        EditDirectorInput(variant_snapshot=_snapshot()),
        ctx=RunContext(extra={"skip_agent_run_persist": True}),
    )

    assert len(output.suggestions) == 3
    assert EditDirectorAgent.spec.enable_json_repair is True
    assert EditDirectorFallbackAgent.spec.enable_json_repair is True


def test_director_review_latency_has_a_bounded_primary_and_fallback_budget() -> None:
    assert EditDirectorAgent.spec.max_attempts == 1
    assert EditDirectorAgent.spec.timeout_s <= 30.0
    assert EditDirectorFallbackAgent.spec.max_attempts == 1
    assert EditDirectorFallbackAgent.spec.timeout_s <= 20.0


def test_director_prompt_includes_exact_operation_field_contract() -> None:
    snapshot = _snapshot()
    snapshot["allowed_op_families"].append("sfx")
    prompt = EditDirectorAgent(ModelClient()).render_prompt(
        EditDirectorInput(variant_snapshot=snapshot)
    )

    assert '{"op":"set_text_timing","bar_index":0,"start_s":0.2,"end_s":2.8}' in prompt
    assert '{"op":"set_clip_duration","slot_index":1,"duration_s":3.0}' in prompt
    assert (
        '{"op":"add_sfx","effect_id":"sfx_pop","at_s":1.2,"gain":1.0,"effect_bundle_id":"reveal_1"}'
    ) in prompt
    assert (
        '{"op":"set_transition","boundary_index":0,'
        '"transition":"crossfade","duration_s":0.3}' in prompt
    )


def test_director_prompt_omits_unavailable_operation_families() -> None:
    snapshot = _snapshot()
    snapshot["allowed_op_families"] = ["text", "clip", "sfx", "music", "title"]

    prompt = EditDirectorAgent(ModelClient()).render_prompt(
        EditDirectorInput(variant_snapshot=snapshot)
    )

    assert '{"op":"set_clip_duration"' in prompt
    assert '{"op":"add_sfx"' in prompt
    assert '{"op":"set_transition"' not in prompt
    assert '{"op":"add_camera_effect"' not in prompt
    assert '{"op":"set_visual_fade"' not in prompt


def test_director_filters_dismissed_ids_without_reordering_remaining() -> None:
    initial = _parse(_valid_suggestions())
    dismissed = initial.suggestions[1].id

    output = _parse(_valid_suggestions(), dismissed_suggestion_ids=[dismissed])

    assert [item.title for item in output.suggestions] == [
        "Sharpen the opening promise",
        "Add a restrained hook pulse",
        "Clarify the working title",
    ]


def test_director_dismissal_survives_rewording_of_the_same_target() -> None:
    initial = _parse(_valid_suggestions())
    rephrased = _valid_suggestions()
    rephrased[0]["title"] = "A different title for the same hook edit"
    rephrased[0]["ops"][0]["text"] = "Different wording, same text target"

    output = _parse(rephrased[:3], dismissed_suggestion_ids=[initial.suggestions[0].id])

    assert [item.title for item in output.suggestions] == [
        "Tighten the second beat",
        "Add a restrained hook pulse",
    ]


def test_director_allows_visual_fades_but_never_server_render_ops() -> None:
    visual = _suggestion(
        "effect",
        "Fade the visual card in",
        {
            "op": "set_visual_fade",
            "visual_block_index": 0,
            "transition_in": "fade",
        },
    )
    output = _parse([*_valid_suggestions()[:3], visual])
    assert output.suggestions[-1].ops == [
        {
            "op": "set_visual_fade",
            "visual_block_index": 0,
            "transition_in": "fade",
        }
    ]

    server_render = _suggestion(
        "effect",
        "Change the intro layout",
        {"op": "set_intro_layout", "layout": "cluster"},
    )
    output = _parse([server_render, *_valid_suggestions()[:2]])
    assert [item.title for item in output.suggestions] == [
        "Sharpen the opening promise",
        "Tighten the second beat",
    ]


def test_director_drops_an_invalid_bundle_but_preserves_valid_siblings() -> None:
    suggestions = _valid_suggestions()[:3]
    suggestions[0]["ops"].append(
        {"op": "set_transition", "boundary_index": 99, "transition": "flash"}
    )

    output = _parse(suggestions)
    assert [item.title for item in output.suggestions] == [
        "Tighten the second beat",
        "Add a restrained hook pulse",
    ]


def test_director_returns_zero_when_no_valid_suggestions_remain() -> None:
    invalid = _suggestion(
        "effect",
        "Change the intro layout",
        {"op": "set_intro_layout", "layout": "cluster"},
    )

    assert _parse([invalid]).suggestions == []


def test_director_suppresses_conflicting_targets_without_discarding_low_diversity_cards() -> None:
    conflicting = _suggestion(
        "hook_pacing",
        "Try a different opening promise",
        {"op": "edit_text", "bar_index": 0, "text": "A conflicting hook"},
    )
    output = _parse([_valid_suggestions()[0], conflicting, *_valid_suggestions()[1:]])
    assert "Try a different opening promise" not in {
        suggestion.title for suggestion in output.suggestions
    }

    one_category = _valid_suggestions()[:3]
    for suggestion in one_category:
        suggestion["category"] = "hook_pacing"
    output = _parse(one_category)
    assert len(output.suggestions) == 3
    assert {suggestion.category for suggestion in output.suggestions} == {"hook_pacing"}


@pytest.mark.parametrize(
    "later_timeline_op",
    [
        {"op": "set_clip_duration", "slot_index": 1, "duration_s": 2.0},
        {"op": "set_clip_in", "slot_index": 1, "in_s": 0.2},
        {"op": "remove_clip", "slot_index": 1},
        {"op": "split_clip", "slot_index": 1, "at_s": 4.0},
        {"op": "set_transition", "boundary_index": 1, "transition": "flash"},
    ],
)
def test_director_returns_at_most_one_sequentially_sensitive_timeline_card(
    later_timeline_op: dict,
) -> None:
    reorder = _suggestion(
        "hook_pacing",
        "Lead with the reveal",
        {"op": "reorder_clip", "from_index": 2, "to_index": 0},
    )
    later = _suggestion("hook_pacing", "Also reshape another beat", later_timeline_op)

    output = _parse([reorder, later, _valid_suggestions()[0]])

    assert [suggestion.title for suggestion in output.suggestions] == [
        "Lead with the reveal",
        "Sharpen the opening promise",
    ]


def test_director_drops_an_internally_conflicting_timeline_bundle() -> None:
    conflicting = _suggestion(
        "hook_pacing",
        "Remove and reconnect the opening",
        {"op": "remove_clip", "slot_index": 0},
    )
    conflicting["ops"].append({"op": "set_transition", "boundary_index": 0, "transition": "flash"})

    output = _parse([conflicting, _valid_suggestions()[0]])

    assert [suggestion.title for suggestion in output.suggestions] == [
        "Sharpen the opening promise"
    ]


def test_director_omni_requires_flag_and_explicit_bounded_source() -> None:
    omni = {
        **_suggestion("effect", "Restyle the reveal", {"op": "edit_text"}),
        "apply_mode": "omni_async",
        "ops": [],
        "omni": {
            "action": "restyle_segment",
            "prompt": "Turn this selected reveal into a restrained film-burn bridge.",
            "insert_at_s": 3.0,
            "duration_s": 4.0,
            "source_clip_index": 1,
            "source_start_s": 0.0,
            "source_end_s": 3.0,
        },
    }
    second_omni = json.loads(json.dumps(omni))
    second_omni["title"] = "Restyle the opening"
    second_omni["omni"]["source_clip_index"] = 0
    suggestions = [omni, second_omni, *_valid_suggestions()[:3]]

    disabled = _parse(suggestions, omni_enabled=False)
    assert all(item.apply_mode == "instant" for item in disabled.suggestions)

    enabled = _parse(suggestions, omni_enabled=True)
    assert len(enabled.suggestions) == 1
    assert enabled.suggestions[0].apply_mode == "omni_async"
    assert enabled.suggestions[0].omni is not None
    assert enabled.suggestions[0].omni.source_clip_index == 1

    instant_first = _parse([*_valid_suggestions()[:3], omni], omni_enabled=True)
    assert all(item.apply_mode == "instant" for item in instant_first.suggestions)

    omni["omni"]["source_end_s"] = 11.0
    second_omni["omni"]["source_end_s"] = 11.0
    over_limit = _parse(suggestions, omni_enabled=True)
    assert all(item.apply_mode == "instant" for item in over_limit.suggestions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_enabled", "client_enabled", "expected"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
async def test_director_omni_requires_server_and_client_capability(
    monkeypatch,
    server_enabled: bool,
    client_enabled: bool,
    expected: bool,
) -> None:
    output = _parse(_valid_suggestions()[:1])
    observed: list[bool] = []

    def primary(self, agent_input, *, ctx=None):  # noqa: ANN001, ARG001
        observed.append(agent_input.omni_enabled)
        return output

    monkeypatch.setattr(_director.settings, "omni_generated_video_enabled", server_enabled)
    monkeypatch.setattr(_director.EditDirectorAgent, "run", primary)

    await _director.run_director(
        _director.DirectorSuggestionsBody(
            snapshot=_snapshot(),
            snapshot_revision=f"revision-{server_enabled}-{client_enabled}",
            omni_enabled=client_enabled,
        ),
        job_id=uuid.uuid4(),
    )

    assert observed == [expected]


@pytest.mark.asyncio
async def test_director_falls_back_from_pro_to_flash(monkeypatch) -> None:
    output = _parse(_valid_suggestions()[:3])

    def primary_failure(*args, **kwargs):  # noqa: ARG001
        from app.agents._runtime import TerminalError

        raise TerminalError("pro temporarily unavailable")

    monkeypatch.setattr(_director.EditDirectorAgent, "run", primary_failure)
    monkeypatch.setattr(
        _director.EditDirectorFallbackAgent,
        "run",
        lambda *args, **kwargs: output,
    )

    response = await _director.run_director(
        _director.DirectorSuggestionsBody(
            snapshot=_snapshot(),
            snapshot_revision="revision-1",
        ),
        job_id=uuid.uuid4(),
    )

    assert response.requested_model == _director.settings.edit_director_model
    assert response.model_used == _director.settings.edit_director_fallback_model
    assert response.fallback_reason == "TerminalError"


@pytest.mark.asyncio
async def test_director_skips_fallback_when_a_newer_snapshot_supersedes_primary(
    monkeypatch,
) -> None:
    job_id = uuid.uuid4()
    fallback = Mock()

    def primary_failure(*args, **kwargs):  # noqa: ARG001
        from app.agents._runtime import TerminalError

        _director._latest_revision_by_job[str(job_id)] = "revision-2"
        raise TerminalError("stale primary")

    monkeypatch.setattr(_director.EditDirectorAgent, "run", primary_failure)
    monkeypatch.setattr(_director.EditDirectorFallbackAgent, "run", fallback)

    with pytest.raises(HTTPException) as caught:
        await _director.run_director(
            _director.DirectorSuggestionsBody(
                snapshot=_snapshot(),
                snapshot_revision="revision-1",
            ),
            job_id=job_id,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "edit_director_request_superseded"
    fallback.assert_not_called()
