"""Unit tests for ``_build_template_text_fixture`` in export_eval_fixtures.py.

The exporter reconstructs a TemplateTextAgent flat-list fixture by walking
post-PR-#188 ``VideoTemplate.recipe_cached.slots[*].text_overlays`` (where the
agent's output was merged in by ``template_text_extraction._merge_overlays_into_slots``).

This test runs the canonical merge into synthetic slots, then re-exports via
the new builder, and asserts identity (slot_index, sample_text, color, role,
effect, size_class, bbox geometry, and global timings within 1e-6).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.agents._schemas.template_text import (
    TemplateTextOutput,
    TemplateTextOverlay,
    TextBBox,
)
from app.tasks.template_text_extraction import _merge_overlays_into_slots

_API_ROOT = Path(__file__).resolve().parents[2]
_EXPORT_SCRIPT = _API_ROOT / "scripts" / "export_eval_fixtures.py"


def _load_export_module():
    spec = importlib.util.spec_from_file_location("export_eval_fixtures", str(_EXPORT_SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTemplate:
    """Stand-in for VideoTemplate ORM row — only the fields the builder reads."""

    def __init__(self, *, recipe_cached, id_="tpl_test_123", name="Round Trip Test", gcs_path=None):
        self.id = id_
        self.name = name
        self.gcs_path = gcs_path
        self.recipe_cached = recipe_cached


def test_round_trip_identity_through_merge():
    """Merge an agent output into slots then reconstruct — every field must match."""
    eef = _load_export_module()
    agent_output = [
        TemplateTextOverlay(
            slot_index=1,
            sample_text="Hook!",
            start_s=0.2,
            end_s=2.5,
            bbox=TextBBox(x_norm=0.5, y_norm=0.2, w_norm=0.6, h_norm=0.1, sample_frame_t=1.0),
            font_color_hex="#FFD700",
            effect="pop-in",
            role="hook",
            size_class="large",
        ),
        TemplateTextOverlay(
            slot_index=2,
            sample_text="Label",
            start_s=3.5,
            end_s=6.0,
            bbox=TextBBox(x_norm=0.5, y_norm=0.8, w_norm=0.3, h_norm=0.05, sample_frame_t=4.5),
            font_color_hex="#FFFFFF",
            effect="fade-in",
            role="label",
            size_class="medium",
        ),
        TemplateTextOverlay(
            slot_index=2,
            sample_text="Sub",
            start_s=4.5,
            end_s=5.5,
            bbox=TextBBox(x_norm=0.3, y_norm=0.85, w_norm=0.2, h_norm=0.04, sample_frame_t=5.0),
            font_color_hex="#CCCCCC",
            effect="static",
            role="label",
            size_class="small",
        ),
    ]
    slots = [
        {"target_duration_s": 3.0, "text_overlays": []},
        {"target_duration_s": 4.0, "text_overlays": []},
    ]
    merged = _merge_overlays_into_slots(slots, agent_output)
    assert merged == 3

    tpl = _FakeTemplate(
        recipe_cached={"slots": slots, "creative_direction": "test"},
        gcs_path="templates/tpl_test_123/reference.mp4",
    )
    fixture = eef._build_template_text_fixture(tpl)
    assert fixture is not None
    assert fixture["agent"] == "nova.compose.template_text"
    assert fixture["input"]["file_uri"] == "templates/tpl_test_123/reference.mp4"
    assert fixture["input"]["slot_boundaries_s"] == [(0.0, 3.0), (3.0, 7.0)]

    recon = fixture["output"]["overlays"]
    assert len(recon) == 3
    for orig, r in zip(agent_output, recon):
        assert r["slot_index"] == orig.slot_index
        assert r["sample_text"] == orig.sample_text
        assert abs(r["start_s"] - orig.start_s) < 1e-6
        assert abs(r["end_s"] - orig.end_s) < 1e-6
        assert abs(r["bbox"]["sample_frame_t"] - orig.bbox.sample_frame_t) < 1e-6
        assert r["bbox"]["x_norm"] == orig.bbox.x_norm
        assert r["bbox"]["y_norm"] == orig.bbox.y_norm
        assert r["bbox"]["w_norm"] == orig.bbox.w_norm
        assert r["bbox"]["h_norm"] == orig.bbox.h_norm
        assert r["font_color_hex"] == orig.font_color_hex
        assert r["effect"] == orig.effect
        assert r["role"] == orig.role
        assert r["size_class"] == orig.size_class

    # The reconstructed output must be a valid TemplateTextOutput.
    parsed = TemplateTextOutput.model_validate(fixture["output"])
    assert len(parsed.overlays) == 3


def test_recipe_only_template_returns_none():
    """Pre-PR-#188 templates (no _extracted_by marker) must be skipped."""
    eef = _load_export_module()
    tpl = _FakeTemplate(
        recipe_cached={
            "slots": [
                {
                    "target_duration_s": 3.0,
                    "text_overlays": [
                        {"sample_text": "old", "start_s": 0, "end_s": 2, "role": "hook"}
                    ],
                }
            ]
        }
    )
    assert eef._build_template_text_fixture(tpl) is None


def test_no_slots_returns_none():
    eef = _load_export_module()
    tpl = _FakeTemplate(recipe_cached={"creative_direction": "x"})
    assert eef._build_template_text_fixture(tpl) is None


def test_none_recipe_returns_none():
    eef = _load_export_module()
    tpl = _FakeTemplate(recipe_cached=None)
    assert eef._build_template_text_fixture(tpl) is None


def test_overlays_missing_bbox_filtered_out():
    """An overlay with _extracted_by but no text_bbox should be silently dropped."""
    eef = _load_export_module()
    tpl = _FakeTemplate(
        recipe_cached={
            "slots": [
                {
                    "target_duration_s": 3.0,
                    "text_overlays": [
                        {
                            "_extracted_by": "nova.compose.template_text",
                            "sample_text": "no bbox",
                            "start_s": 0,
                            "end_s": 1,
                            # text_bbox intentionally missing
                        }
                    ],
                }
            ]
        }
    )
    # No bbox → no valid overlays → fixture is None.
    assert eef._build_template_text_fixture(tpl) is None


def test_degenerate_slot_is_skipped_in_boundary_cursor():
    """A slot with target_duration_s=0 is skipped — cursor does not advance."""
    eef = _load_export_module()
    # Build via the canonical merge so the timing math comes from production code.
    agent_output = [
        TemplateTextOverlay(
            slot_index=1,
            sample_text="hello",
            start_s=0.5,
            end_s=1.5,
            bbox=TextBBox(x_norm=0.5, y_norm=0.5, w_norm=0.2, h_norm=0.1, sample_frame_t=1.0),
            font_color_hex="#FFFFFF",
        ),
    ]
    slots = [
        {"target_duration_s": 2.0, "text_overlays": []},
        {"target_duration_s": 0.0, "text_overlays": []},  # degenerate
        {"target_duration_s": 3.0, "text_overlays": []},
    ]
    _merge_overlays_into_slots(slots, agent_output)

    tpl = _FakeTemplate(recipe_cached={"slots": slots})
    fixture = eef._build_template_text_fixture(tpl)
    assert fixture is not None
    # Degenerate slot dropped from boundaries; remaining two boundaries are
    # contiguous from 0.
    assert fixture["input"]["slot_boundaries_s"] == [(0.0, 2.0), (2.0, 5.0)]
