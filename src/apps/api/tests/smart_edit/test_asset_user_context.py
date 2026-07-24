"""Smart edit visual matching prefers creator-authored asset context."""

from __future__ import annotations

from types import SimpleNamespace

from app.smart_edit.planner import _coerce_assets, _matching_assets
from app.smart_edit.schemas import SmartWord


def _word(text: str, index: int) -> SmartWord:
    return SmartWord(
        word_id=f"w{index:06d}",
        spoken_text=text,
        display_text=text,
        normalized_text=text.casefold(),
        start_ms=index * 100,
        end_ms=index * 100 + 80,
        timing_quality="aligned",
        display_alignment=[f"w{index:06d}"],
    )


def test_coerce_assets_keeps_user_context_separate_from_analysis() -> None:
    assets = _coerce_assets(
        [
            {
                "id": "a1",
                "kind": "image",
                "user_context": "This is the churn dashboard",
                "analysis": {"subject": "chart", "description": "Nova sees revenue"},
            }
        ]
    )
    assert assets[0].user_context == "This is the churn dashboard"
    assert assets[0].description == "Nova sees revenue"


def test_matching_assets_anchors_on_user_context_when_nova_metadata_is_generic() -> None:
    assets = _coerce_assets(
        [
            {
                "id": "a1",
                "kind": "image",
                "user_context": "churn dashboard",
                "analysis": {"subject": "chart", "description": "generic graph"},
            }
        ]
    )
    matches = _matching_assets(
        [_word("the", 1), _word("churn", 2), _word("dashboard", 3)],
        assets,
        SimpleNamespace(visual_aliases=[]),
    )
    assert matches == [("a1", "w000002")]
