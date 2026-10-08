"""Structural checks for redacted, offline replays of KRI-524 model captures.

The fixture records model provenance and raw responses, but these tests make
claims only after running the recorded operations through the real parser,
compiler, and draft projector. The earlier captures use synthetic ``title``
roles, so save validation is explicitly unverified until canonical captures are
available. No render or export result is implied by a passing test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests.evals.runners.snapshot_variant import build_synthetic_job, build_synthetic_variant

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "prompt_coverage" / "live_composition.json"
)


@pytest.fixture(scope="module")
def captures() -> dict[str, dict]:
    payload = json.loads(FIXTURE.read_text())
    return {row["case_id"]: row for row in payload["cases"]}


def _replay(row: dict):
    agent = EditCopilotAgent(ModelClient())
    parsed = agent.parse(
        row["raw_output"],
        EditCopilotInput(
            utterance=row["prompt"],
            prior_turns=[],
            variant_snapshot=row["input_snapshot"],
        ),
    )
    variant = build_synthetic_variant(row["input_snapshot"], parsed.ops)
    job = build_synthetic_job(variant)
    compiled = compile_editor_ops(job, variant, parsed.ops).payload
    return parsed, variant, job, compiled


def _assert_source_slots_preserved(before: dict, after: dict) -> None:
    before_slots = before["input_snapshot"]["slots"]
    after_slots = after["ai_timeline"]["slots"]
    keys = (
        "slot_id",
        "parent_segment_id",
        "clip_index",
        "in_s",
        "duration_beats",
        "duration_s",
        "source_duration_s",
        "output_start_s",
        "output_end_s",
        "removed",
        "transition_after",
        "transition_duration_s",
        "look_preset",
        "media_kind",
    )
    assert [{key: slot.get(key) for key in keys} for slot in after_slots] == [
        {key: slot.get(key) for key in keys} for slot in before_slots
    ]


def test_live_sequence_captures_compile_with_independent_word_and_phrase_expectations(captures):
    for case_id, expected in [
        (
            "kri524-live-sequence_words",
            [
                "Come",
                "with",
                "me",
                "to",
                "my",
                "favorite",
                "restaurant",
                "and",
                "ice",
                "cream",
                "place",
                "in",
                "Lisbon.",
            ],
        ),
        ("kri524-live-sequence_phrases", ["Morning coffee.", "Afternoon walk.", "Evening lights."]),
        ("kri524-live-heldout_chunks", ["North wind,", "clear sky,", "calm water."]),
        ("kri524-live-heldout_correction", ["Small", "steps", "make", "great", "journeys."]),
    ]:
        row = captures[case_id]
        parsed, variant, job, payload = _replay(row)
        assert row["provenance"]["kind"] == "live_model_capture"
        assert parsed.outcome == "proposed" and len(parsed.ops) == 1
        rows = list(payload.text_elements)
        assert [item["text"] for item in rows] == expected
        expected_windows = [
            (
                index * row["expected"]["expected_duration"] / len(expected),
                (index + 1) * row["expected"]["expected_duration"] / len(expected),
            )
            for index in range(len(expected))
        ]
        assert len(rows) == len(expected_windows)
        for actual, wanted in zip(rows, expected_windows, strict=True):
            assert actual["start_s"] == pytest.approx(wanted[0])
            assert actual["end_s"] == pytest.approx(wanted[1])
        assert all(item["animation_phases"]["entrance"] == "fade" for item in rows)
        assert all(item["animation_phases"]["exit"] == "fade" for item in rows)
        projected = project_editor_draft(variant, payload.model_dump(mode="json"))
        _assert_source_slots_preserved(row, projected)
        assert row.get("save_evidence") == "unverified_synthetic_title_role"


def test_live_persistent_and_placement_captures_preserve_text_and_source(captures):
    row = captures["kri524-live-persistent"]
    parsed, variant, job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    bars = list(payload.text_elements)
    assert [(bar["start_s"], bar["end_s"]) for bar in bars[:2]] == [(0.0, 12.0), (0.0, 12.0)]
    assert [(bar["x_frac"], bar["y_frac"], bar["alignment"]) for bar in bars[:2]] == [
        (0.08, 0.82, "left"),
        (0.08, 0.88, "left"),
    ]
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    assert row.get("save_evidence") == "unverified_synthetic_title_role"

    row = captures["kri524-live-placement"]
    parsed, variant, job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    bar = payload.text_elements[0]
    assert bar["y_frac"] == 0.85
    assert (bar["x_frac"], bar["start_s"], bar["end_s"]) == (0.5, 0.0, 2.0)
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    assert row.get("save_evidence") == "unverified_synthetic_title_role"


def test_baseline_style_only_capture_does_not_count_as_sequence_success(captures):
    row = captures["kri524-live-sequence_baseline"]
    parsed, variant, _job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    # Historical capture a applied only a style effect; it did not compose
    # sequence children. Keep this failure visible beside the passing capture b.
    assert len(payload.text_elements) == 1
    assert payload.text_elements[0]["text"] == row["input_snapshot"]["text_bars"][0]["text"]


def test_negative_and_no_effect_captures_keep_their_recorded_outcomes(captures):
    row = captures["kri524-live-wholebar_negative"]
    parsed, _variant, _job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    assert len(payload.text_elements) == 1
    assert payload.text_elements[0]["text"] == row["expected"]["expected_text"]

    row = captures["kri524-live-already_satisfied"]
    parsed = EditCopilotAgent(ModelClient()).parse(
        row["raw_output"],
        EditCopilotInput(
            utterance=row["prompt"],
            prior_turns=[],
            variant_snapshot=row["input_snapshot"],
        ),
    )
    assert parsed.outcome == "no_effect"
    assert parsed.ops == []

    row = captures["kri524-live-compound_failed"]
    parsed, _variant, _job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    # Capture c is retained as historical negative evidence: it changed color
    # and effect but failed to emit the requested word sequence. Capture d is
    # the separate passing compound response.
    assert len(payload.text_elements) == 1
    assert payload.text_elements[0]["text"] == row["input_snapshot"]["text_bars"][0]["text"]

    row = captures["kri524-live-whole_edit_preserved"]
    parsed, variant, _job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    assert payload.text_elements[0]["color"] == "#FFFFFF"
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    assert row.get("save_evidence") == "unverified_synthetic_title_role"


def test_final_compound_capture_composes_words_and_color(captures):
    row = captures["kri524-live-compound_final"]
    parsed, variant, job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    rows = list(payload.text_elements)
    assert [item["text"] for item in rows] == row["expected"]["segments"]
    assert all(item["color"] == row["expected"]["expected_color"] for item in rows)
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    assert row.get("save_evidence") == "unverified_synthetic_title_role"


def test_canonical_captures_replay_and_validate_real_save_without_role_rewrite(captures):
    cases = {
        "kri524-canonical-heldout_chunks": ["North wind,", "clear sky,", "calm water."],
        "kri524-canonical-heldout_correction": ["Small", "steps", "make", "great", "journeys."],
        "kri524-canonical-compound": ["Every", "corner", "tells", "a", "story."],
        "kri524-canonical-exact_correction": ["A", "little", "adventure", "starts", "here."],
    }
    for case_id, expected_segments in cases.items():
        row = captures[case_id]
        parsed, variant, job, payload = _replay(row)
        assert parsed.outcome == "proposed"
        assert [item["text"] for item in payload.text_elements] == expected_segments
        assert row["save_evidence"] == "passed"
        projected = project_editor_draft(variant, payload.model_dump(mode="json"))
        _assert_source_slots_preserved(row, projected)
        gj.prepare_editor_commit(job, variant["variant_id"], payload)


def test_canonical_persistent_and_preservation_captures_save_exact_state(captures):
    row = captures["kri524-canonical-persistent_final"]
    parsed, variant, job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    assert [(bar["start_s"], bar["end_s"]) for bar in payload.text_elements[:2]] == [
        (0.0, 12.0),
        (0.0, 12.0),
    ]
    assert [(bar["x_frac"], bar["y_frac"]) for bar in payload.text_elements[:2]] == [
        (0.08, 0.8),
        (0.08, 0.86),
    ]
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    gj.prepare_editor_commit(job, variant["variant_id"], payload)

    row = captures["kri524-canonical-whole_edit_preserved"]
    parsed, variant, job, payload = _replay(row)
    assert parsed.outcome == "proposed"
    assert payload.text_elements[0]["color"] == "#FFFFFF"
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    _assert_source_slots_preserved(row, projected)
    gj.prepare_editor_commit(job, variant["variant_id"], payload)


def test_whole_chain_capture_keeps_extraction_and_editor_layers_distinct(captures):
    chain = json.loads(FIXTURE.read_text())["whole_chain_capture"]
    assert chain["provenance"]["layer"] == "brief_extractor_then_editor_router"
    assert chain["brief_output"]["brief_updates"] == []
    assert chain["route"] == "editor_ops"
    assert chain["editor_input"]["utterance"] == chain["input"]["user_message"]
    assert chain["editor_output"]["outcome"] == "proposed"
    assert chain["save_validation"] == "passed"
    ops = chain["editor_output"]["ops"]
    assert len(ops) == 1 and ops[0]["op"] == "replace_text_sequence"
    assert ops[0]["segments"] == [
        "Come",
        "with",
        "me",
        "to",
        "my",
        "favorite",
        "restaurant",
        "and",
        "ice",
        "cream",
        "place",
        "in",
        "Lisbon.",
    ]
