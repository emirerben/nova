"""Creation -> persisted composition -> canonical timeline; offline model replay."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agents.edit_copilot import EditCopilotInput
from app.pipeline.guided_story import (
    GuidedStoryError,
    compile_proposal_execution_plan,
    validate_execution_plan,
)
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot

TITLE = "Join us for our favorite bakery and tea shop near the harbor"


def base_plan():
    return plan_unified_montage(
        [
            UnifiedClip(
                media_id=f"c{i}",
                proxy_path=f"users/test/c{i}.mp4",
                generation="1",
                duration_s=12,
                width=1080,
                height=1920,
            )
            for i in range(2)
        ],
        BriefView(
            title_literal=TITLE,
            wants_per_clip_text=True,
            placeholder_labels=True,
            target_duration_s=20,
        ),
        strategy={"opening_title": TITLE, "font_family": "Inter", "text_color": "#FFFFFF"},
        font_covers=lambda *_: True,
    )


def sequence_response():
    return {
        "intent": "edit",
        "confidence": 0.95,
        "reply": "Prepared the requested title sequence.",
        "ops": [
            {
                "op": "set_texts_timing",
                "selector": {"ids": ["guided-title"]},
                "start_s": 0,
                "end_s": 7,
            },
            {
                "op": "replace_text_sequence",
                "selector": {"ids": ["guided-title"]},
                "segments": TITLE.split(),
                "patch": {
                    "animation_phases": {
                        "entrance": "fade",
                        "exit": "fade",
                        "loop": "none",
                        "speed": 1,
                    }
                },
            },
        ],
    }


def compose(monkeypatch, plan, response=None):
    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.creation_text_composition import compose_creation_text

    seen = []

    def run(self, input, *, ctx=None):
        seen.append(input)
        assert isinstance(input, EditCopilotInput)
        return self.parse(json.dumps(response or sequence_response()), input)

    monkeypatch.setattr(EditCopilotAgent, "run", run)
    result = compose_creation_text(
        plan.snapshot,
        creator_request=(
            f"Title: {TITLE}. Show each word separately, fade in and out, "
            "then the next. Keep location labels."
        ),
    )
    assert len(seen) == 1
    assert "each word separately" in seen[0].utterance
    assert "gcs_path" not in json.dumps(seen[0].variant_snapshot)
    return result


def test_creation_sequence_survives_snapshot_reload_and_canonical_validation(monkeypatch):
    plan = base_plan()
    original = compile_proposal_execution_plan(plan.snapshot)
    snapshot = compose(monkeypatch, plan)
    restored = EditProposalSnapshot.model_validate_json(snapshot.model_dump_json())
    result = compile_proposal_execution_plan(restored)
    words = [x for x in result["text_elements"] if "::sequence-" in x["id"]]
    assert [x["text"] for x in words] == TITLE.split()
    assert len(words) == len(TITLE.split())
    assert words[0]["start_s"] == 0 and words[-1]["end_s"] == 7
    assert all(a["end_s"] == b["start_s"] for a, b in zip(words, words[1:]))
    assert all(x["font_family"] == "Inter" and x["color"] == "#FFFFFF" for x in words)
    assert all(
        x["animation_phases"]["entrance"] == "fade" and x["animation_phases"]["exit"] == "fade"
        for x in words
    )
    assert result["story_timeline"] == original["story_timeline"]
    assert [x for x in result["text_elements"] if x["id"].startswith("clip-label-")] == [
        x for x in original["text_elements"] if x["id"].startswith("clip-label-")
    ]
    plan.snapshot = restored
    assert validate_execution_plan(result, plan.guided_edit())
    tampered = copy.deepcopy(result)
    tampered["text_elements"][0]["text"] = "wrong"
    with pytest.raises(GuidedStoryError):
        validate_execution_plan(tampered, plan.guided_edit())


def test_arbitrary_phrase_composition_and_unchanged_creation(monkeypatch):
    plan = base_plan()
    response = sequence_response()
    response["ops"][1]["segments"] = [
        "Join us",
        "for our favorite bakery",
        "and tea shop near the harbor",
    ]
    snapshot = compose(monkeypatch, plan, response)
    texts = compile_proposal_execution_plan(snapshot)["text_elements"]
    assert [x["text"] for x in texts if "::sequence-" in x["id"]] == response["ops"][1]["segments"]
    unchanged = compose(
        monkeypatch,
        plan,
        {"intent": "describe", "confidence": 0.95, "reply": "Already matches.", "ops": []},
    )
    assert compile_proposal_execution_plan(unchanged) == compile_proposal_execution_plan(
        plan.snapshot
    )


def test_changed_base_is_rejected_and_existing_program_is_not_replanned(monkeypatch):
    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.creation_text_composition import compose_creation_text

    snapshot = compose(monkeypatch, base_plan())
    monkeypatch.setattr(
        EditCopilotAgent, "run", lambda *a, **kw: pytest.fail("Pinned composition called a model")
    )
    assert compose_creation_text(snapshot, creator_request="again") == snapshot
    changed = snapshot.model_copy(update={"font_family": "Anton"})
    with pytest.raises(ValueError, match="composition"):
        compile_proposal_execution_plan(changed)


def test_partial_or_invalid_composition_is_not_silently_accepted(monkeypatch):
    response = sequence_response()
    response["unmet_requests"] = [{"request": "sequence", "reason": "cannot"}]
    with pytest.raises(ValueError, match="composition"):
        compose(monkeypatch, base_plan(), response)


def test_disallowed_operations_cannot_change_source_timeline(monkeypatch):
    response = {
        "intent": "edit",
        "confidence": 0.95,
        "reply": "Trimmed",
        "ops": [{"op": "set_clip_duration", "clip_index": 0, "duration_s": 1}],
    }
    with pytest.raises(ValueError, match="composition"):
        compose(monkeypatch, base_plan(), response)


def test_duplicate_hook_is_corrected_without_dropping_location_label(monkeypatch):
    plan = base_plan()
    plan.snapshot.clip_labels[0].text = TITLE
    response = sequence_response()
    response["ops"].insert(
        0, {"op": "rewrite_text", "selector": {"ids": ["clip-label-unified-cut-1"]}, "text": "Name"}
    )
    snapshot = compose(monkeypatch, plan, response)
    texts = compile_proposal_execution_plan(snapshot)["text_elements"]
    assert not any(row["text"] == TITLE for row in texts)
    assert [row["text"] for row in texts if row["id"].startswith("clip-label-")] == ["Name", "Name"]


def test_added_text_has_repeatable_ids_and_timing(monkeypatch):
    response = {
        "intent": "edit",
        "confidence": 0.99,
        "reply": "Prepared",
        "ops": [
            {
                "op": "add_text",
                "text": "Later",
                "start_s": 8,
                "end_s": 10,
                "patch": {"font_family": "Inter", "animation_phases": {"entrance": "fade"}},
            },
        ],
    }
    snapshot = compose(monkeypatch, base_plan(), response)
    assert compile_proposal_execution_plan(snapshot) == compile_proposal_execution_plan(snapshot)


def test_created_words_remain_editable_for_faster_followup(monkeypatch):
    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.creation_text_composition import _lanes
    from app.services.kria_editor_ops import apply_text_lane_ops

    snapshot = compose(monkeypatch, base_plan())
    before = compile_proposal_execution_plan(snapshot)
    texts, slots = _lanes(before)
    words = [r for r in texts if "::sequence-" in r["id"]]
    raw = {
        "intent": "edit",
        "confidence": 0.99,
        "reply": "Prepared",
        "ops": [
            {
                "op": "set_texts_timing",
                "selector": {"ids": [r["id"]]},
                "start_s": r["start_s"] / 2,
                "end_s": r["end_s"] / 2,
            }
            for r in words
        ],
    }
    output = EditCopilotAgent(None).parse(
        json.dumps(raw),
        EditCopilotInput(
            utterance="Make those words twice as fast; keep everything else.",
            variant_snapshot={
                "editor_ops_version": 2,
                "allowed_op_families": ["text"],
                "text_bars": texts,
                "slots": slots,
                "total_duration_s": 20,
            },
        ),
    )
    state = apply_text_lane_ops(texts, copy.deepcopy(slots), output.ops)
    after = [r for r in state.text if "::sequence-" in r["id"]]
    assert [r["text"] for r in after] == TITLE.split()
    assert after[-1]["end_s"] == 3.5
    assert all(r["animation_phases"] == words[i]["animation_phases"] for i, r in enumerate(after))
    assert state.slots == slots
    assert [r for r in state.text if r["id"].startswith("clip-label-")] == [
        r for r in texts if r["id"].startswith("clip-label-")
    ]


def test_phone_export_recipe_contains_each_created_word(monkeypatch):
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.guided_story import GuidedStoryExecutionPlan
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan
    from app.services.phone_sources import PhoneSourceBinding

    snapshot = compose(monkeypatch, base_plan())
    plan = GuidedStoryExecutionPlan.model_validate(compile_proposal_execution_plan(snapshot))
    bindings = tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"users/test/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=("a" if i == 0 else "b") * 64,
                byte_count=1000,
                duration_s=12,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for i in range(2)
    )
    # Bind the same approved identities as real uploaded analysis proxies.
    for row in plan.story_timeline:
        row.gcs_path = next(b.proxy_path for b in bindings if b.media_id == row.media_id)
    recipe = compile_phone_guided_plan(plan, bindings)
    data = recipe.model_dump(mode="json")
    assert len(data["text_layers"]) >= len(TITLE.split())


def test_receipt_uses_compiled_text_not_superseded_base_labels(monkeypatch):
    plan = base_plan()
    response = {
        "intent": "edit",
        "confidence": 0.99,
        "reply": "Prepared",
        "ops": [
            {"op": "rewrite_text", "selector": {"group": "labels"}, "text": "Location"},
        ],
    }
    plan.snapshot = compose(monkeypatch, plan, response)
    assert [row["text"] for row in plan.record()["labels"]] == ["Location", "Location"]


def test_creation_model_sees_complete_long_title_and_currency(monkeypatch):
    from app.services.creation_text_composition import CreationTextComposer

    title = "Only $12 — " + "a carefully chosen neighborhood favorite " * 6
    title = title[:275].strip()
    plan = base_plan()
    plan.snapshot.opening_title = title
    seen = []

    def run(self, input, *, ctx=None):
        seen.append(self.render_prompt(input))
        return self.parse(
            json.dumps(
                {"intent": "describe", "confidence": 1.0, "reply": "Already satisfied", "ops": []}
            ),
            input,
        )

    monkeypatch.setattr(CreationTextComposer, "run", run)
    from app.services.creation_text_composition import compose_creation_text

    compose_creation_text(plan.snapshot, creator_request="Keep the title unchanged.")
    assert title in seen[0]


def test_catalog_music_does_not_retime_persisted_text_composition(monkeypatch):
    from app.pipeline.guided_story import compile_execution_plan

    plan = base_plan()
    plan.snapshot = compose(monkeypatch, plan)
    original = compile_proposal_execution_plan(plan.snapshot)
    with_music = compile_execution_plan(
        plan.guided_edit(),
        track={
            "track_id": "track-1",
            "title": "Beat",
            "audio_gcs_path": "music/beat.mp3",
            "generation": "1",
            "start_s": 0.0,
            "beat_timestamps_s": [9.8, 19.8],
        },
    )
    assert with_music["story_timeline"] == original["story_timeline"]
    assert with_music["text_elements"] == original["text_elements"]


_CAPTURE = json.loads(
    (Path(__file__).parents[2] / "fixtures/prompt_coverage/creation_composition.json").read_text()
)


@pytest.mark.parametrize("case", _CAPTURE["cases"], ids=lambda case: case["id"])
def test_captured_creation_program_meets_independent_request_expectations(monkeypatch, case):
    from app.services.creation_text_composition import CreationTextComposer, compose_creation_text

    def run(self, input, *, ctx=None):
        return self.parse(case["model_response"], input)

    monkeypatch.setattr(CreationTextComposer, "run", run)
    base = EditProposalSnapshot.model_validate(case["snapshot"])
    original = compile_proposal_execution_plan(base)
    composed = compose_creation_text(base, creator_request=case["request"])
    restored = EditProposalSnapshot.model_validate_json(composed.model_dump_json())
    actual = compile_proposal_execution_plan(restored)
    expected = case["expected"]
    words = [row for row in actual["text_elements"] if "::sequence-" in row["id"]]
    assert [row["text"] for row in words] == expected["chunks"]
    assert all(a["end_s"] <= b["start_s"] for a, b in zip(words, words[1:]))
    assert all(row["end_s"] > row["start_s"] for row in words)
    assert all(row["animation_phases"]["entrance"] == expected["entrance"] for row in words)
    assert all(row["animation_phases"]["exit"] == expected["exit"] for row in words)
    assert all(row["font_family"] == "Inter" and row["color"] == "#FFFFFF" for row in words)
    labels = [row for row in actual["text_elements"] if row["id"].startswith("clip-label-")]
    if "labels" in expected:
        assert [row["text"] for row in labels] == expected["labels"]
    if expected.get("label_region") == "bottom_left":
        assert len(labels) == len(original["story_timeline"])
        assert all(row["text"] and row["text"] != TITLE for row in labels)
        assert all(0 <= row["x_frac"] <= 0.3 and 0.7 <= row["y_frac"] <= 1 for row in labels)
        assert all(row["alignment"] == "left" for row in labels)
    if expected.get("opening_clip_only"):
        assert words[0]["start_s"] >= 0
        assert words[-1]["end_s"] <= original["story_timeline"][0]["output_end_s"]
    assert not any(row["text"] == TITLE for row in actual["text_elements"])
    assert actual["story_timeline"] == original["story_timeline"]


def test_creation_program_is_persisted_but_not_public_proposal_response(monkeypatch):
    from app.schemas.edit_proposal import EditProposalSnapshotResponse

    snapshot = compose(monkeypatch, base_plan())
    stored = snapshot.model_dump(mode="json")
    assert stored["text_composition"]["operations"]
    response = EditProposalSnapshotResponse.model_validate(stored)
    assert "text_composition" not in response.model_dump(mode="json")
    assert "text_composition" not in EditProposalSnapshotResponse.model_json_schema()["properties"]
    assert "text_composition" not in EditProposalSnapshot.model_json_schema()["properties"]
