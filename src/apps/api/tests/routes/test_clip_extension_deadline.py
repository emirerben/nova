"""Delayed model transport → parsed extension → Save → production phone recipe.

The transport is authored, with a simulated 55s response. This is not evidence
of live Gemini latency or reliability. Initial words/media are synthetic.
"""

import copy
import json
import time

import pytest

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError
from app.routes import _copilot as copilot
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.routes import test_compound_word_trim_save as trim
from tests.services._guided_timeline_fixtures import label_bar


async def _extension_journey(monkeypatch, *, captured_shape=False):
    captured = []
    original = trim._guided_shape_job

    def capture(mp):
        job, revision = original(mp)
        captured.append(job)
        return job, revision

    monkeypatch.setattr(trim, "_guided_shape_job", capture)
    trim.test_twelve_explicit_words_survive_compound_trim_save_and_later_save(monkeypatch)
    job = captured[0]
    job.all_candidates = {}
    variant = job.assembly_plan["variants"][0]
    initial = build_editor_snapshot(job, variant)
    label_slot = initial["slots"][1]
    label = label_bar(label_slot["media_id"], label_slot["slot_id"], 2, 3, "Next stop")
    # Include an already-aligned label: this auxiliary operation caused the
    # captured live model's valid clip/text edits to be discarded by parsing.
    label["y_frac"] = 0.15
    trim._guided_save(job, {}, text_elements=[*variant["text_elements"], label])
    variant = job.assembly_plan["variants"][0]
    before = copy.deepcopy(variant)
    snapshot = build_editor_snapshot(job, variant)
    # Eleven 0.2-second words, then hold the final word for 0.8 seconds.
    starts = [i / 5 for i in range(12)]
    ends = [(i + 1) / 5 for i in range(11)] + [3.0]
    operations = [
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 3}},
        *[
            {"op": "set_text_timing", "bar_index": i, "start_s": start, "end_s": end}
            for i, (start, end) in enumerate(zip(starts, ends, strict=True))
        ],
    ]
    if captured_shape:
        starts = [row["start_s"] for row in before["text_elements"][:12]]
        ends = [row["end_s"] for row in before["text_elements"][:11]] + [3.0]
        operations = [
            {"op": "set_clip_duration", "slot_index": 0, "duration_s": 3.0},
            {"op": "set_text_timing", "bar_index": 11, "start_s": starts[-1], "end_s": 3.0},
        ]
    operations.append({"op": "realign_labels"})
    calls = []

    class DelayedTransport:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            if kwargs["timeout_s"] < 55:
                raise ProviderOutcomeUnknownError("simulated response exceeds caller deadline")
            return ModelInvocation(
                raw_text=json.dumps(
                    {
                        "intent": "edit",
                        "confidence": 0.99,
                        "reply": "Prepared the extension.",
                        "ops": operations,
                    }
                ),
            )

    monkeypatch.setattr(copilot, "default_client", DelayedTransport)
    response = await copilot.run_copilot_turn(
        copilot.CopilotTurnBody(
            message="Extend the opening to three seconds and hold the last word longer.",
            snapshot=snapshot,
            client_contract_version=2,
        ),
        job_id=job.id,
        deadline_monotonic=time.monotonic() + 170,
        timeout_override_s=120,
    )
    assert len(calls) == 1
    assert calls[0]["thinking_level"] == "high"
    assert response.outcome == "proposed", response.model_dump()
    assert len(response.ops) == (2 if captured_shape else 13)
    assert variant == before, "model planning must not mutate the saved edit"
    compiled = compile_editor_ops(job, variant, response.ops)
    gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    recipe = device_status(job, "guided_story").request.recipe
    assert recipe.duration == pytest.approx(5), "later one-second clips must survive"
    word_layers = [row for row in recipe.text_layers if "::sequence-" in row.id]
    if captured_shape:
        assert word_layers[0].end == pytest.approx(ends[0]), "preserve short opening words"
    label_layer = next(row for row in recipe.text_layers if "::sequence-" not in row.id)
    assert [row.start for row in word_layers] == pytest.approx(
        starts, abs=1 / 30 if captured_shape else 1e-6
    )
    assert [row.end for row in word_layers] == pytest.approx(
        ends, abs=1 / 30 if captured_shape else 1e-6
    )
    assert word_layers[11].end - word_layers[11].start == pytest.approx(3 - starts[-1])
    assert label_layer.start == 3
    assert label_layer.end == 4, "labels must follow the extended clip"
    rows = job.assembly_plan["variants"][0]["text_elements"]
    for previous, current in zip(before["text_elements"], rows, strict=True):
        for key in ("id", "text", "x_frac", "y_frac", "font_family", "animation_phases"):
            assert current[key] == previous[key]


@pytest.mark.asyncio
async def test_delayed_clip_extension_and_longer_last_word_survive_save(monkeypatch):
    await _extension_journey(monkeypatch)


@pytest.mark.asyncio
async def test_recorded_compound_extension_survives_parse_save_and_phone_recipe(monkeypatch):
    """Live operation shape; synthetic wording/media and preserved initial windows."""
    await _extension_journey(monkeypatch, captured_shape=True)
