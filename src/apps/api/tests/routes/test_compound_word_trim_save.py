"""Offline replay of the reported compound trim through the actual Save compiler.

The operation shape and windows come from the captured turn; the media, wording
and identities are synthetic. This does not establish live model reliability.
"""

import copy

import pytest

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.kria_editor_ops import compile_editor_ops
from tests._prod_profile import apply_prod_profile
from tests.routes.test_phone_editor_commit import _guided_save, _guided_shape_job


def test_twelve_explicit_words_survive_compound_trim_save_and_later_save(monkeypatch):
    apply_prod_profile(monkeypatch)
    job, revision = _guided_shape_job(monkeypatch)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    words = "Join us for our favorite bakery and tea shop near the harbor".split()
    # The real edit had a deleted/merged word. Stable sequence IDs are allowed
    # to contain that gap; timing edits must not renumber the surviving bars.
    ids = [f"guided-title::sequence-{ordinal}" for ordinal in [*range(1, 10), 11, 12, 13]]
    bars = [
        {
            "id": ids[index],
            "text": word,
            "start_s": index * 3 / 12,
            "end_s": (index + 1) * 3 / 12,
            "role": "generative_sequence",
            "font_family": "Inter",
            "size_px": 72,
            "x_frac": 0.3,
            "y_frac": 0.7,
            "position": "custom",
            "effect": "fade-in",
            "animation_phases": {"entrance": "fade", "exit": "fade", "loop": "none", "speed": 1},
            "source_params": {"sequence_source_id": "guided-title"},
        }
        for index, word in enumerate(words)
    ]
    _guided_save(
        job,
        revision,
        text_elements=bars,
        timeline_slots=[
            gj.TimelineSlotEdit(
                slot_id=row["segment_id"],
                clip_index=0,
                in_s=row["source_start_s"],
                duration_s=3 if index == 0 else 1,
            )
            for index, row in enumerate(revision["segments"])
        ],
    )
    variant = job.assembly_plan["variants"][0]
    baseline = copy.deepcopy(variant)
    starts = [0, 0.16, 0.30, 0.46, 0.62, 0.76, 0.92, 1.08, 1.22, 1.54, 1.70, 1.84]
    ends = [0.15, 0.30, 0.46, 0.62, 0.76, 0.92, 1.08, 1.22, 1.52, 1.70, 1.84, 2]
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 2}},
            *[
                {"op": "set_text_timing", "bar_index": i, "start_s": start, "end_s": end}
                for i, (start, end) in enumerate(zip(starts, ends, strict=True))
            ],
        ],
    )
    assert variant == baseline, "draft compilation must not commit the edit"
    prep = gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    assert prep["render_destination"] == "device"

    def assert_saved():
        request = device_status(job, "guided_story").request
        assert request.recipe.duration == pytest.approx(4), "two later one-second clips survive"
        rows = job.assembly_plan["variants"][0]["text_elements"]
        assert len(rows) == 12
        for index, row in enumerate(rows):
            assert row["id"] == ids[index]
            assert row["text"] == words[index]
            assert row["start_s"] == pytest.approx(starts[index])
            assert row["end_s"] == pytest.approx(ends[index])
            assert (row["x_frac"], row["y_frac"], row["font_family"]) == (0.3, 0.7, "Inter")
        layers = request.recipe.text_layers
        assert len(layers) == 12
        assert [layer.start for layer in layers] == pytest.approx(starts)
        assert [layer.end for layer in layers] == pytest.approx(ends)

    assert_saved()
    # An unrelated follow-up Save must not rebase these already-saved windows.
    _guided_save(job, revision, mix=gj.EditorCommitMix(original_level=0.4))
    assert_saved()
