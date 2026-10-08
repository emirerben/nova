"""Executable KRI-524 journey cassettes and corpus linkage.

The cassettes exercise the real v2 agent adapter/compiler offline. Their authored model
responses are inputs, not evidence that a model would choose the operations; a bounded live
run can reuse the same prompts and snapshot later.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.kria_editor_ops import compile_editor_ops
from tests.evals.runners.snapshot_variant import build_synthetic_variant_and_job

from .models import FinalPlan, RFFixture, Turn
from .runner import replay_turns

CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "prompt_coverage" / "corpus.json"
POSITIONING_CASE_ID = "kri524-montage-creation-positioning"
POSITIONING_PROMPT = (
    "Put “Free things to do” above “Part 1” in the lower-left, and “[Viewpoint]” in the upper-left."
)


def _snapshot() -> dict:
    return {
        "base_generation": "kri524-journey-gen",
        "total_duration_s": 12.0,
        "text_bars": [
            {
                "id": "title-main",
                "role": "title",
                "text": "Free things to do",
                "start_s": 0.0,
                "end_s": 2.0,
                "position": "middle",
                "alignment": "center",
                "x_frac": 0.5,
                "y_frac": 0.5,
            },
            {
                "id": "title-sub",
                "role": "title",
                "text": "Part 1",
                "start_s": 0.0,
                "end_s": 2.0,
                "position": "middle",
                "alignment": "center",
                "x_frac": 0.5,
                "y_frac": 0.5,
            },
            {
                "id": "location",
                "role": "label",
                "text": "Viewpoint",
                "start_s": 2.0,
                "end_s": 5.0,
                "position": "custom",
                "alignment": "left",
                "x_frac": 0.12,
                "y_frac": 0.16,
            },
        ],
        "slots": [
            {"slot_id": "shot-1", "clip_index": 0, "in_s": 1.0, "duration_s": 6.0},
            {"slot_id": "shot-2", "clip_index": 1, "in_s": 4.0, "duration_s": 6.0},
        ],
    }


def _turn(turn_id: str, prompt: str, model_output: dict) -> Turn:
    return Turn(
        turn_id=turn_id,
        user_message=prompt,
        engine="v2_kria",
        kria={
            "cassette": {
                "agent_input": {
                    "utterance": prompt,
                    "variant_snapshot": _snapshot(),
                    "prior_turns": [],
                },
                "model_output": model_output,
            }
        },
    )


def _fixture(turns: list[Turn]) -> RFFixture:
    return RFFixture(
        fixture_id="kri524-journeys",
        provenance="authored",
        footage="food_day",
        turns=turns,
        requirements=[],
        reference={"plan_after": FinalPlan().model_dump()},
    )


def test_executed_positioning_prompt_matches_its_corpus_case():
    rows = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    by_id = {row["case_id"]: row for row in rows}
    assert by_id[POSITIONING_CASE_ID]["prompt"] == POSITIONING_PROMPT


def test_positioning_cassette_runs_agent_adapter_and_compiler():
    prompt = POSITIONING_PROMPT
    direct_job, direct_variant = build_synthetic_variant_and_job(_snapshot())
    direct = compile_editor_ops(
        direct_job,
        direct_variant,
        [{"op": "patch_text_style", "bar_index": 0, "patch": {"x_frac": 0.12}}],
    )
    assert direct.payload.text_elements[0]["x_frac"] == 0.12
    result = replay_turns(
        _fixture(
            [
                _turn(
                    "positioning",
                    prompt,
                    {
                        "intent": "edit",
                        "ops": [
                            {"op": "set_text_timing", "bar_index": 0, "start_s": 0, "end_s": 12},
                            {"op": "set_text_timing", "bar_index": 1, "start_s": 0, "end_s": 12},
                            {
                                "op": "patch_text_style",
                                "bar_index": 0,
                                "patch": {
                                    "position": "custom",
                                    "alignment": "left",
                                    "x_frac": 0.12,
                                },
                            },
                            {
                                "op": "patch_text_style",
                                "bar_index": 1,
                                "patch": {
                                    "position": "custom",
                                    "alignment": "left",
                                    "x_frac": 0.12,
                                },
                            },
                            {"op": "patch_text_style", "bar_index": 0, "patch": {"y_frac": 0.86}},
                            {"op": "patch_text_style", "bar_index": 1, "patch": {"y_frac": 0.94}},
                            {
                                "op": "patch_text_style",
                                "bar_index": 2,
                                "patch": {
                                    "position": "custom",
                                    "alignment": "left",
                                    "x_frac": 0.12,
                                    "y_frac": 0.16,
                                },
                            },
                        ],
                        "confidence": 1.0,
                        "reply": "Applied the requested text layout.",
                        "suggestions": [],
                        "needs_clarification": False,
                        "clarification_context": None,
                        "pending_actions": [],
                    },
                )
            ]
        )
    )[0]
    assert result.execution_proof and result.execution_proof.model_call_observed
    assert result.plan_after.texts[0].start_s == 0
    assert result.plan_after.texts[0].end_s == 12
    # Regression: the normal agent -> adapter path must carry the requested left
    # coordinate into the compiler result, just as direct compilation does.
    assert result.plan_after.texts[0].x_frac == 0.12
    assert result.plan_after.texts[2].y_frac == 0.16


def test_unsupported_and_ambiguous_cassettes_keep_state_and_speak_honestly():
    result = replay_turns(
        _fixture(
            [
                _turn(
                    "unsupported",
                    "Keep the whole source video but rebuild its edit from chat.",
                    {
                        "intent": "unsupported",
                        "ops": [],
                        "confidence": 0.99,
                        "reply": "I can't safely rebuild the full source edit from chat yet.",
                        "suggestions": [],
                        "needs_clarification": False,
                        "clarification_context": None,
                        "pending_actions": [],
                    },
                ),
                _turn(
                    "ambiguous",
                    "Put the title at the top.",
                    {
                        "intent": "edit",
                        "ops": [],
                        "confidence": 0.2,
                        "reply": (
                            "Which top area do you mean: upper-left, upper-right, or centered?"
                        ),
                        "suggestions": [],
                        "needs_clarification": True,
                        "clarification_context": "position",
                        "pending_actions": [],
                    },
                ),
            ]
        )
    )
    assert result[0].plan_after == result[1].plan_after
    assert "can't" in result[0].reply.lower()
    assert "which top area" in result[1].reply.lower()
