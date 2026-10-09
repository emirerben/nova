"""KRI-559 composed-title journey replay.

This is a synthetic/redacted replay of the KRI-524 failure chain.  Model
responses are authored transport fixtures; parsing, persisted-plan reload,
validation, and phone compilation use production code.  It does not make a
provider call, write a database row, or claim native playback evidence.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.cloud_render_contract import CloudRenderContractError, check_guided_plan_text
from app.services.creation_text_composition import _lanes
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.kria_editor_ops import apply_text_lane_ops
from app.services.phone_sources import PhoneSourceBinding

from .test_creation_composition import TITLE, base_plan, compose, sequence_response

FIXTURE_PROVENANCE = "authored_synthetic_redaction_of_kri524_composed_title_incident"


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=f"c{index}",
            proxy_path=f"users/test/analysis-proxy-c{index}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=("a" if index == 0 else "b") * 64,
                byte_count=1_000,
                duration_s=12,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for index in range(2)
    )


def _phone_recipe(compiled: dict, bindings: tuple[PhoneSourceBinding, ...]):
    plan = GuidedStoryExecutionPlan.model_validate(compiled)
    for moment in plan.story_timeline:
        moment.gcs_path = next(
            binding.proxy_path for binding in bindings if binding.media_id == moment.media_id
        )
    return compile_phone_guided_plan(plan, bindings)


def _contract() -> CreatorRenderContract:
    return CreatorRenderContract(
        generation_id="redacted-kri559-creation",
        original_audio="require",
        exact_texts=(
            TextRequirement(role="opening", text=TITLE),
            TextRequirement(role="any", text=TITLE),
        ),
    ).rebind()


def test_composed_title_journey_replays_creation_retry_reopen_and_faster_followup(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    """Keep title proof and unrelated video/audio through every replayed boundary."""
    assert FIXTURE_PROVENANCE.startswith("authored_synthetic_redaction")
    bindings = _bindings()
    contract = _contract()
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}

    # New creation: production composition, then its serialized snapshot
    # boundary. This represents reopening state but is deliberately not a DB-save
    # assertion; database persistence has separate coverage.
    created = compose(monkeypatch, base_plan(), sequence_response())
    reopened = EditProposalSnapshot.model_validate_json(created.model_dump_json())
    compiled = compile_proposal_execution_plan(reopened)
    original_timeline = copy.deepcopy(compiled["story_timeline"])
    original_labels = [
        copy.deepcopy(row)
        for row in compiled["text_elements"]
        if row["id"].startswith("clip-label-")
    ]

    # The captured failure was a successful composition rejected at rendering.
    # Deliberately remove proof and require both production gates to decline it.
    rejected = copy.deepcopy(compiled)
    rejected_words = [row for row in rejected["text_elements"] if "::sequence-" in row["id"]]
    rejected["text_elements"].remove(rejected_words[1])
    with pytest.raises(CloudRenderContractError):
        check_guided_plan_text(assembly, candidates=None, plan=rejected)
    with pytest.raises(CreatorRenderContractError):
        verify_phone_recipe(
            contract,
            _phone_recipe(rejected, bindings),
            source_audio={"c0": True, "c1": True},
        )

    # Retrying the failed attempt uses the saved, reopened composition.  Both
    # gates accept actual wording, word order, timing/visibility provenance, and
    # source-audio preservation before a phone recipe is handed to the client.
    check_guided_plan_text(assembly, candidates=None, plan=compiled)
    recipe = _phone_recipe(compiled, bindings)
    assert verify_phone_recipe(contract, recipe, source_audio={"c0": True, "c1": True})
    words = [row for row in compiled["text_elements"] if "::sequence-" in row["id"]]
    assert [row["text"] for row in words] == TITLE.split()
    assert words[0]["start_s"] == 0
    assert words[-1]["end_s"] == 7
    assert all(left["end_s"] == right["start_s"] for left, right in zip(words, words[1:]))

    # Same-chat follow-up: parse the faster request against the persisted lanes,
    # apply it with the production editor operation compiler, and reopen that
    # state once more.  The edit may retime title children only.
    texts, slots = _lanes(compiled)
    faster = EditCopilotAgent(None).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 1.0,
                "reply": "Made the title words twice as fast.",
                "ops": [
                    {
                        "op": "set_texts_timing",
                        "selector": {"ids": [word["id"]]},
                        "start_s": index * 7 / len(words) / 2,
                        "end_s": (index + 1) * 7 / len(words) / 2,
                    }
                    for index, word in enumerate(words)
                ],
            }
        ),
        EditCopilotInput(
            utterance="Make those words twice as fast. Keep the fades and everything else.",
            variant_snapshot={
                "editor_ops_version": 2,
                "allowed_op_families": ["text"],
                "text_bars": texts,
                "slots": slots,
                "total_duration_s": 20,
            },
        ),
    )
    after = apply_text_lane_ops(texts, copy.deepcopy(slots), faster.ops)
    after_words = [row for row in after.text if "::sequence-" in row["id"]]
    assert [row["text"] for row in after_words] == TITLE.split()
    assert after_words[-1]["end_s"] == 3.5
    assert all(row["animation_phases"]["entrance"] == "fade" for row in after_words)
    assert after.slots == slots
    assert [
        (
            slot["media_id"],
            slot["source_start_s"],
            slot["source_end_s"],
            slot["output_start_s"],
            slot["output_end_s"],
        )
        for slot in after.slots
    ] == [
        (
            row["media_id"],
            row["source_start_s"],
            row["source_end_s"],
            row["output_start_s"],
            row["output_end_s"],
        )
        for row in original_timeline
    ]
    assert [row for row in after.text if row["id"].startswith("clip-label-")] == original_labels


def test_unsupported_compound_followup_cannot_be_counted_as_verified() -> None:
    """A parser refusal produces no operations, so no validation proof exists."""
    outcome = EditCopilotAgent(None).parse(
        json.dumps(
            {
                "intent": "edit",
                "confidence": 1.0,
                "reply": "I changed the whole video and made the words faster.",
                "ops": [{"op": "restyle_all", "preset": "cinematic"}],
            }
        ),
        EditCopilotInput(
            utterance=(
                "Make the title words twice as fast, keep the fades, video, and audio, "
                "then rebuild the whole edit."
            ),
            variant_snapshot={"editor_ops_version": 2, "allowed_op_families": ["text"]},
        ),
    )
    assert outcome.outcome == "unsupported"
    assert outcome.ops == []
