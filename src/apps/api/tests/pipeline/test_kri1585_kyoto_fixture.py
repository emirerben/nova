"""Kyoto Torokko (prod job 3930c822): the shipped research rows, read by today's resolver.

With INFORMATIVE_TEXT_DESIGNS_ENABLED off these must keep holding: research facts are filed as
titles, so the saved title pref (top) beats the creator's all_text bottom_left.
"""

import json
from pathlib import Path

import pytest

from app.pipeline.phone_text_style import text_role
from app.schemas.phone_text_style import effective_text_rule

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1585_kyoto"


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def test_kyoto_research_rows_resolve_as_titles_placed_top():
    expected = _load("expected_rows.json")["baseline"]
    rows = _load("text_rows.json")["text_elements"]
    style = _load("plan.json")["variant"]["phone_text_style"]

    assert [row["id"] for row in rows] == expected["row_ids"]
    assert {text_role(row) for row in rows} == {expected["text_role"]}
    rule = effective_text_rule(style["preferences"], style["requested"], expected["text_role"])
    assert rule["position"] == expected["resolved_title_position"]
    assert style["preferences"]["all_text"]["position"] == expected["creator_all_text_position"]


CACHE = Path(__file__).resolve().parents[5] / ".cache" / "kri1585-kyoto"


@pytest.mark.skipif(
    not (CACHE / "frames.json").exists(),
    reason="needs footage frames: python scripts/kri1585_kyoto_fixture.py cache",
)
def test_kyoto_cached_frames_reproduce_shipped_fills():
    """Black, black, white: the sampler's pick over the cached proxy frames, as shipped."""
    from app.pipeline.phone_text_background import automatic_fill
    from app.pipeline.phone_text_style import measure_box

    frames = json.loads((CACHE / "frames.json").read_text(encoding="utf-8"))["frames"]
    rows = _load("text_rows.json")["text_elements"]
    fills = [
        automatic_fill(
            measure_box(row),
            [
                {"pixels": (CACHE / f["gray"]).read_bytes()}
                for f in frames
                if f["window"] == row["id"]
            ],
        )[0]
        for row in rows
    ]
    assert fills == _load("expected_rows.json")["baseline"]["colors"]
