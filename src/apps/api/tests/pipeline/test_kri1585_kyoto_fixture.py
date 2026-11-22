"""Kyoto Torokko (prod job 3930c822): the shipped research rows, read by today's resolver.

With INFORMATIVE_TEXT_DESIGNS_ENABLED off these must keep holding: research facts are filed as
titles, so the saved title pref (top) beats the creator's all_text bottom_left.
"""

import json
from pathlib import Path

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
