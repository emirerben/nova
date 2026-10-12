"""KRI-589: the freehand-drawing wire contract, against the fixture Swift also decodes.

``tests/fixtures/drawing-parity/v1.json`` holds elements that MUST validate (every texture
plus edge cases) and entries that MUST be rejected. The Swift ``EditorDrawing`` decoder is
tested against the same file, so a rule changed on one side only fails one of the two suites.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents._schemas.drawing import (
    MAX_DRAWINGS,
    MAX_POINTS_PER_DRAWING,
    MAX_POINTS_PER_STROKE,
    MAX_POINTS_PER_VARIANT,
    MAX_STROKES_PER_DRAWING,
    DrawingElement,
    coerce_drawings,
    validate_drawings,
)

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "drawing-parity" / "v1.json"
    ).read_text()
)
ELEMENTS = FIXTURE["elements"]


def _drawing(drawing_id: str = "d", *, strokes: int = 1, points: int = 2, **overrides) -> dict:
    stroke = {
        "color": "#FF3B30",
        "width_frac": 0.012,
        "points": [[0.1 + 0.0001 * i, 0.2] for i in range(points)],
    }
    return {
        "id": drawing_id,
        "start_s": 0.0,
        "end_s": 2.0,
        "canvas_aspect": 0.5625,
        "strokes": [copy.deepcopy(stroke) for _ in range(strokes)],
        **overrides,
    }


def test_fixture_covers_every_texture() -> None:
    textures = {stroke["texture"] for element in ELEMENTS for stroke in element["strokes"]}
    assert textures == {"pen", "marker", "highlighter", "neon", "chalk", "spray"}


@pytest.mark.parametrize("element", ELEMENTS, ids=[e["id"] for e in ELEMENTS])
def test_valid_fixture_elements_validate_and_round_trip(element: dict) -> None:
    validated = validate_drawings([element], duration_s=10.0)
    assert validated == [DrawingElement.model_validate(element).model_dump(mode="json")]
    assert coerce_drawings([element])[0].id == element["id"]


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=[c["name"] for c in FIXTURE["invalid"]])
def test_invalid_fixture_entries_are_rejected(case: dict) -> None:
    with pytest.raises(ValidationError):
        DrawingElement.model_validate(case["element"])
    # A tolerant read drops exactly the bad row and keeps its neighbours.
    assert [d.id for d in coerce_drawings([case["element"], ELEMENTS[0]])] == [ELEMENTS[0]["id"]]


def test_a_write_can_not_carry_a_drawing_past_the_video() -> None:
    with pytest.raises(ValueError, match="exceeds the variant duration"):
        validate_drawings([_drawing(end_s=5.0)], duration_s=2.0)
    # ...but one frame of float noise at the end is not a violation.
    validate_drawings([_drawing(end_s=2.04)], duration_s=2.0)


def test_ids_must_be_unique_and_a_missing_id_is_assigned() -> None:
    with pytest.raises(ValueError, match="unique"):
        validate_drawings([_drawing("a"), _drawing("a")], duration_s=10.0)
    anonymous = {k: v for k, v in _drawing().items() if k != "id"}
    assert validate_drawings([anonymous], duration_s=10.0)[0]["id"]


def test_caps_are_named_in_the_error() -> None:
    with pytest.raises(ValueError, match=f"Maximum {MAX_DRAWINGS} drawings"):
        validate_drawings([_drawing(f"d{i}") for i in range(MAX_DRAWINGS + 1)], duration_s=10.0)
    with pytest.raises(ValidationError):
        DrawingElement.model_validate(_drawing(strokes=MAX_STROKES_PER_DRAWING + 1))
    with pytest.raises(ValidationError):
        DrawingElement.model_validate(_drawing(points=MAX_POINTS_PER_STROKE + 1))
    with pytest.raises(ValidationError, match=f"exceeds {MAX_POINTS_PER_DRAWING} points"):
        DrawingElement.model_validate(_drawing(strokes=3, points=MAX_POINTS_PER_DRAWING // 3 + 1))
    per_drawing = MAX_POINTS_PER_DRAWING
    count = MAX_POINTS_PER_VARIANT // per_drawing + 1
    heavy = [_drawing(f"h{i}", strokes=2, points=per_drawing // 2) for i in range(count)]
    with pytest.raises(ValueError, match=f"exceed {MAX_POINTS_PER_VARIANT} points"):
        validate_drawings(heavy, duration_s=10.0)


def test_color_is_normalised_and_points_are_rounded() -> None:
    drawing = _drawing()
    drawing["strokes"][0]["color"] = "#ff3b30"
    drawing["strokes"][0]["points"] = [[0.123456, 0.654321], [0.2, 0.3]]
    stroke = validate_drawings([drawing], duration_s=10.0)[0]["strokes"][0]
    assert stroke["color"] == "#FF3B30"
    assert stroke["points"][0] == [0.1235, 0.6543]
