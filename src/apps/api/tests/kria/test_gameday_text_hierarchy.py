"""KRI-669 failures, specified before implementing the layout.

The real creation compiler must preserve title/subtitle/detail sizes and grouping;
long copy must wrap without independently shrinking a member; a second unrelated
stack in the same corner must stay separate; disjoint windows may reuse space;
wording/position receipts must never certify an unfulfilled smaller/below ask;
legacy pins must retain their old rows. The production incident used these exact
three strings and initially compiled every member at 52px.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_editor_payload
from app.pipeline.canvas import PORTRAIT
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.pinned_text import pinned_text_elements, resolve_pin_windows
from app.pipeline.portable_text_layout import compile_text_overlay
from app.schemas.edit_proposal import PinnedText
from tests.kria.test_pinned_texts import _compiled, _pinned
from tests.kria.test_pinned_texts import _plan as _fixture_plan

COPY = ["Gameday", "Aston Villa v Fenerbahçe", "6 am"]


def _plan(strategy):
    plan = _fixture_plan(strategy)
    plan.snapshot = plan.snapshot.model_copy(update={"output_orientation": "portrait"})
    return plan


def _pins(*, corner="top_left", text=None):
    return [
        {"text": value, "corner": corner, "heading_level": i + 1, "stack_id": "match"}
        for i, value in enumerate(text or COPY)
    ]


def _rows(pins):
    return pinned_text_elements(
        resolve_pin_windows([PinnedText.model_validate(pin) for pin in pins], 21.038),
        font_family="Fraunces",
        text_color=None,
    )


def _bounds(row):
    from app.pipeline.text_stack_layout import measure_text_block, row_canvas

    height = measure_text_block(row)["height"]
    y = row["y_frac"] * row_canvas(row).height
    return y - height / 2, y + height / 2


def test_incident_compiles_three_levels_with_tighter_subtitle_detail_group():
    plan = _plan({"pinned_texts": _pins()})
    rows = _pinned(_compiled(plan))
    assert [row["text"] for row in rows] == COPY
    assert [row["size_px"] for row in rows] == [72, 52, 40]
    assert {(row["x_frac"], row["alignment"]) for row in rows} == {(0.08, "left")}
    bounds = [_bounds(row) for row in rows]
    assert bounds[1][0] - bounds[0][1] == pytest.approx(24, abs=0.2)
    assert bounds[2][0] - bounds[1][1] == pytest.approx(8, abs=0.2)
    assert all(
        row["start_s"] == 0 and row["end_s"] == pytest.approx(plan.duration_s) for row in rows
    )
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=plan.duration_s,
        independent_box_alignment=True,
    )
    layers = [
        compile_text_overlay(overlay, layer_id=str(i), canvas=PORTRAIT)[0]
        for i, overlay in enumerate(overlays)
    ]
    assert [layer.runs[0].font_size for layer in layers] == [72, 52, 40]


@pytest.mark.parametrize("corner", ["top_left", "bottom_right"])
def test_wrapped_stack_and_unrelated_stack_stay_separate_and_safe(corner):
    pins = _pins(corner=corner, text=["Gameday", "Aston Villa v Fenerbahçe " * 4, "6 am"])
    pins.append({"text": "Weather", "corner": corner, "heading_level": 2, "stack_id": "weather"})
    rows = _rows(pins)
    bounds = [_bounds(row) for row in rows]
    assert all(192 <= top < bottom <= 1728 for top, bottom in bounds)
    assert all(bounds[i + 1][0] > bounds[i][1] for i in range(3))
    assert bounds[3][0] - bounds[2][1] >= 31.8
    assert rows[0]["size_px"] > rows[1]["size_px"] > rows[2]["size_px"]


def test_nonoverlapping_stacks_reuse_same_corner_space():
    pins = [
        {
            "text": "First",
            "corner": "top_left",
            "heading_level": 1,
            "stack_id": "first",
            "end_s": 5,
        },
        {
            "text": "Second",
            "corner": "top_left",
            "heading_level": 1,
            "stack_id": "second",
            "start_s": 5,
        },
    ]
    a, b = _rows(pins)
    assert a["y_frac"] == b["y_frac"]


def test_explicit_creator_sizes_survive_initial_plan_and_phone_compilation():
    pins = _pins()
    for pin, size in zip(pins, [80, 36, 24], strict=True):
        pin["size_px"] = size
    plan = _plan({"pinned_texts": pins})
    rows = _pinned(_compiled(plan))
    assert [row["size_px"] for row in rows] == [80, 36, 24]
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=plan.duration_s,
        independent_box_alignment=True,
    )
    layers = [
        compile_text_overlay(overlay, layer_id=str(i), canvas=PORTRAIT)[0]
        for i, overlay in enumerate(overlays)
    ]
    assert [layer.runs[0].font_size for layer in layers] == [80, 36, 24]


def test_unrelated_untyped_line_sharing_a_heading_corner_cannot_overlap():
    pins = _pins() + [{"text": "Another label", "corner": "top_left"}]
    rows = _rows(pins)
    assert rows[-1]["size_px"] == 52
    assert _bounds(rows[-1])[0] - _bounds(rows[-2])[1] >= 31.8


def test_shared_width_uses_identical_measured_and_phone_render_line_breaks():
    from app.pipeline.text_stack_layout import measure_text_block

    pins = _pins(text=["Gameday", "AVATAR To Villa", "6 am"])
    pins.append({"text": "Badge", "corner": "top_right", "heading_level": 1, "stack_id": "badge"})
    rows = _rows(pins)
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=21.038,
        independent_box_alignment=True,
    )
    for row, overlay in zip(rows, overlays, strict=True):
        layer, _ = compile_text_overlay(overlay, layer_id=row["id"], canvas=PORTRAIT)
        assert [run.text for run in layer.runs] == measure_text_block(row)["lines"]
        assert layer.runs[0].font_size == row["size_px"]


def test_fitted_stack_still_passes_smaller_and_group_spacing_receipt():
    copy = ["Gameday", "X" * 16, "6 am"]
    pins = _pins(text=copy)
    pins.append({"text": "Badge", "corner": "top_right", "heading_level": 1, "stack_id": "badge"})
    rows = _rows(pins)
    assert rows[1]["size_px"] < 52
    facts = plan_facts_from_editor_payload({"text_elements": rows})
    req = BriefRequirement(
        id="subtitle",
        kind="text",
        scope="global",
        literal=copy[1],
        facts={
            "position": "top_left",
            "heading_level": 2,
            "stack_id": "match",
            "below_text": copy[0],
            "smaller_than": copy[0],
        },
    )
    assert check_requirement(req, facts).status == "met"


def test_opening_title_clears_actual_space_mono_pin_height():
    from app.pipeline.text_stack_layout import measure_text_block

    pins = [
        {
            "text": "AVATAR To Yesterday Villa Fenerbahçe Gameday " * 2,
            "corner": "top_left",
            "heading_level": 1,
            "stack_id": "left",
        },
        {"text": "Badge", "corner": "top_right", "heading_level": 1, "stack_id": "right"},
    ]
    plan = _plan({"pinned_texts": pins})
    snapshot = plan.snapshot.model_copy(
        update={"opening_title": "An opening title", "font_family": "Space Mono"}
    )
    from app.pipeline.guided_story import compile_proposal_execution_plan

    rows = compile_proposal_execution_plan(snapshot)["text_elements"]
    title = next(row for row in rows if row["id"] == "guided-title")
    title_top = title["y_frac"] * 1920 - measure_text_block(title)["height"] / 2
    edge = max(_bounds(row)[1] for row in rows if row["id"].startswith("guided-pinned-"))
    assert title_top > edge


def test_voice_behind_footage_title_clears_the_requested_pin_font():
    from app.pipeline.phone_narrated_plan import with_pinned_text_layers
    from tests.pipeline.test_phone_voice_behind_footage_plan import _compile

    pins = [
        PinnedText(
            text="AVATAR To Yesterday Villa Fenerbahçe Gameday " * 2,
            corner="top_left",
            heading_level=1,
            stack_id="left",
        ),
        PinnedText(text="Badge", corner="top_right", heading_level=1, stack_id="right"),
    ]
    recipe, _ = _compile(
        opening_title="An opening title", pinned_texts=pins, pinned_font_family="Space Mono"
    )
    title = recipe.text_layers[0]
    recipe, rows = with_pinned_text_layers(recipe, pins, font_family="Space Mono")
    edge = max(_bounds(row)[1] for row in rows)
    # Baseline and font ascent keep the title's first line below the pin rectangle.
    assert title.runs[0].baseline_y - title.runs[0].font_size > edge


def test_landscape_creation_recipe_has_actual_24_and_8_pixel_gaps():
    from app.pipeline.canvas import LANDSCAPE
    from app.pipeline.guided_story import compile_proposal_execution_plan
    from app.pipeline.text_stack_layout import measure_text_block

    plan = _plan({"pinned_texts": _pins()})
    snapshot = plan.snapshot.model_copy(update={"output_orientation": "landscape"})
    rows = _pinned(compile_proposal_execution_plan(snapshot))
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=plan.duration_s,
        independent_box_alignment=True,
    )
    layers = [
        compile_text_overlay(overlay, layer_id=str(i), canvas=LANDSCAPE)[0]
        for i, overlay in enumerate(overlays)
    ]
    bounds = [
        (
            layer.anchor_y - measure_text_block(row)["height"] / 2,
            layer.anchor_y + measure_text_block(row)["height"] / 2,
        )
        for row, layer in zip(rows, layers, strict=True)
    ]
    assert bounds[1][0] - bounds[0][1] == pytest.approx(24, abs=0.2)
    assert bounds[2][0] - bounds[1][1] == pytest.approx(8, abs=0.2)
    facts = plan_facts_from_editor_payload(
        {"text_elements": rows, "output_orientation": "landscape"}
    )
    req = BriefRequirement(
        id="subtitle",
        kind="text",
        scope="global",
        literal=COPY[1],
        facts={
            "below_text": COPY[0],
            "smaller_than": COPY[0],
            "heading_level": 2,
            "stack_id": "match",
        },
    )
    assert check_requirement(req, facts).status == "met"


def test_landscape_relative_below_uses_actual_canvas_dimensions():
    from app.pipeline.text_stack_layout import measure_text_block
    from app.services.kria_editor_ops import compile_editor_ops
    from tests.services.test_kria_editor_text_lane import _east_run, _ops, _saved_text

    job, variant, snapshot, _ = _east_run()
    job.assembly_plan["guided_story_execution_plan"]["output_orientation"] = "landscape"
    title = next(row for row in variant["text_elements"] if row["id"] == "guided-title")
    title.update(size_px=72, position="custom", x_frac=0.08, y_frac=0.12, alignment="left")
    output = _ops(
        snapshot,
        [
            {
                "op": "add_text",
                "text": "Supporting detail",
                "start_s": 0,
                "end_s": 3,
                "style_from": "title",
                "below": True,
                "patch": {"size_px": 40},
            }
        ],
    )
    rows = _saved_text(compile_editor_ops(job, variant, output.ops))
    below = next(row for row in rows if row["text"] == "Supporting detail")
    gap = (below["y_frac"] - title["y_frac"]) * 1080 - (
        measure_text_block(title, width=1920)["height"]
        + measure_text_block(below, width=1920)["height"]
    ) / 2
    assert gap == pytest.approx(24, abs=0.2)


def test_legacy_pins_and_serialization_are_unchanged():
    pins = [{"text": value, "corner": "top_left"} for value in COPY]
    rows = _rows(pins)
    assert [row["size_px"] for row in rows] == [52, 52, 52]
    assert [row["y_frac"] for row in rows] == [0.12, 0.158, 0.196]
    assert PinnedText.model_validate(pins[0]).model_dump() == pins[0]


def test_matching_copy_position_cannot_certify_equal_size_subtitle():
    rows = _rows([{"text": value, "corner": "top_left"} for value in COPY])
    facts = plan_facts_from_editor_payload({"text_elements": rows})
    req = BriefRequirement(
        id="subtitle",
        kind="text",
        scope="global",
        literal=COPY[1],
        description="Under Gameday add a smaller title",
        facts={
            "position": "top_left",
            "heading_level": 2,
            "stack_id": "match",
            "below_text": COPY[0],
            "smaller_than": COPY[0],
        },
    )
    assert check_requirement(req, facts).status == "partial"


def test_resolved_hierarchy_satisfies_relationship_receipt():
    rows = _rows(_pins())
    facts = plan_facts_from_editor_payload({"text_elements": rows})
    req = BriefRequirement(
        id="subtitle",
        kind="text",
        scope="global",
        literal=COPY[1],
        description="Under Gameday add a smaller title",
        facts={
            "position": "top_left",
            "heading_level": 2,
            "stack_id": "match",
            "below_text": COPY[0],
            "smaller_than": COPY[0],
        },
    )
    assert check_requirement(req, facts).status == "met"


def test_lost_brief_modifiers_cannot_skip_full_request_composition(monkeypatch):
    from app.services.creation_text_composition import CreationTextComposer, compose_creation_text

    plan = _plan({"pinned_texts": [{"text": value, "corner": "top_left"} for value in COPY]})
    requests = [
        BriefRequirement(
            id=f"r{i}", kind="text", scope="global", literal=value, facts={"position": "top_left"}
        )
        for i, value in enumerate(COPY)
    ]
    called = []

    def compose(self, input, **kwargs):
        called.append(input.utterance)
        raise RuntimeError("composer reached")

    monkeypatch.setattr(CreationTextComposer, "run", compose)
    with pytest.raises(RuntimeError, match="composer reached"):
        compose_creation_text(
            plan.snapshot,
            creator_request="Add a title top left saying Gameday. Under add a smaller title "
            "saying Aston Villa v Fenerbahçe. Under it write 6 am",
            text_requirements=requests,
        )
    assert called


def test_unrelated_typed_badge_cannot_mask_lossy_subtitle_request(monkeypatch):
    from app.services.creation_text_composition import CreationTextComposer, compose_creation_text

    pins = [{"text": value, "corner": "top_left"} for value in COPY]
    pins.append({"text": "Badge", "corner": "top_right", "heading_level": 1, "stack_id": "badge"})
    plan = _plan({"pinned_texts": pins})
    requirements = [
        BriefRequirement(
            id=f"r{i}", kind="text", scope="global", literal=value, facts={"position": "top_left"}
        )
        for i, value in enumerate(COPY)
    ]
    requirements.append(
        BriefRequirement(
            id="badge",
            kind="text",
            scope="global",
            literal="Badge",
            facts={"heading_level": 1, "stack_id": "badge", "position": "top_right"},
        )
    )

    def compose(self, input, **kwargs):
        raise RuntimeError("composer reached")

    monkeypatch.setattr(CreationTextComposer, "run", compose)
    with pytest.raises(RuntimeError, match="composer reached"):
        compose_creation_text(
            plan.snapshot,
            creator_request="Add Gameday top left, a smaller "
            "Aston Villa v Fenerbahçe under it, 6 am under it, and Badge top right",
            text_requirements=requirements,
        )


def test_numeric_size_lost_from_brief_cannot_skip_composition(monkeypatch):
    from app.services.creation_text_composition import CreationTextComposer, compose_creation_text

    plan = _plan({"pinned_texts": [{"text": "Gameday", "corner": "top_left"}]})
    requirement = BriefRequirement(
        id="title", kind="text", scope="global", literal="Gameday", facts={"position": "top_left"}
    )

    def compose(self, input, **kwargs):
        raise RuntimeError("composer reached")

    monkeypatch.setattr(CreationTextComposer, "run", compose)
    with pytest.raises(RuntimeError, match="composer reached"):
        compose_creation_text(
            plan.snapshot,
            creator_request="Add Gameday top left at 80px",
            text_requirements=[requirement],
        )


def test_relative_below_uses_both_measured_blocks_not_constant_canvas_offset():
    from app.pipeline.text_stack_layout import measure_text_block
    from app.services.kria_editor_ops import compile_editor_ops
    from tests.services.test_kria_editor_text_lane import _east_run, _ops, _saved_text

    job, variant, snapshot, _ = _east_run()
    title = next(row for row in variant["text_elements"] if row["id"] == "guided-title")
    title.update(
        text="One long title that takes multiple lines on this canvas",
        size_px=96,
        max_width_frac=0.4,
        position="custom",
        x_frac=0.08,
        y_frac=0.12,
        alignment="left",
    )
    output = _ops(
        snapshot,
        [
            {
                "op": "add_text",
                "text": "Supporting detail",
                "start_s": 0,
                "end_s": 3,
                "style_from": "title",
                "below": True,
                "patch": {"size_px": 40},
            }
        ],
    )
    rows = _saved_text(compile_editor_ops(job, variant, output.ops))
    below = next(row for row in rows if row["text"] == "Supporting detail")
    gap = (
        below["y_frac"] * 1920
        - measure_text_block(below)["height"] / 2
        - (title["y_frac"] * 1920 + measure_text_block(title)["height"] / 2)
    )
    assert gap == pytest.approx(24, abs=1)
