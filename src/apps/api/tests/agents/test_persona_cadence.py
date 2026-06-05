"""Unit tests for resolve_posts_per_week() in _schemas/persona.py.

Tests the fallback chain:
  1. Structured posts_per_week field wins (clamped 1-7).
  2. Regex: max in-range number from posting_cadence prose.
  3. Default: 7 (status quo — one item per day).
"""

from __future__ import annotations

import pytest

from app.agents._schemas.persona import Persona, resolve_posts_per_week


def _persona(cadence: str = "4 posts/week", ppw: int | None = None) -> Persona:
    return Persona(
        summary="s",
        content_pillars=["a"],
        tone="t",
        audience="a",
        posting_cadence=cadence,
        posts_per_week=ppw,
    )


def test_resolve_prefers_structured_field() -> None:
    """Structured field takes precedence over anything in the cadence prose."""
    p = _persona(cadence="daily posting, every day", ppw=3)
    assert resolve_posts_per_week(p) == 3


def test_resolve_structured_field_clamped_high() -> None:
    """posts_per_week=7 is the ceiling; Pydantic rejects 8 before we get here,
    but the clamp in resolve is a safety belt."""
    p = _persona(cadence="every day", ppw=7)
    assert resolve_posts_per_week(p) == 7


def test_resolve_structured_field_clamped_low() -> None:
    p = _persona(cadence="once a week", ppw=1)
    assert resolve_posts_per_week(p) == 1


def test_resolve_parses_cadence_text_range_notation() -> None:
    """'3-4 posts/week' → picks the higher number (4)."""
    p = _persona(cadence="3-4 posts/week, heavier on weekends")
    assert resolve_posts_per_week(p) == 4


def test_resolve_parses_cadence_text_single_number() -> None:
    """'post 3x a week' → 3."""
    p = _persona(cadence="post 3x a week")
    assert resolve_posts_per_week(p) == 3


def test_resolve_ignores_out_of_range_numbers() -> None:
    """Numbers outside 1..7 in cadence text are discarded; falls back to 7."""
    p = _persona(cadence="10 posts/week would be insane")
    assert resolve_posts_per_week(p) == 7


def test_resolve_ignores_mixed_range_keeps_in_range_max() -> None:
    """'2-3 times/week' → 3; the '10' in the sentence is out of range, ignored."""
    p = _persona(cadence="about 10 or 2-3 times/week, flexible")
    # in-range numbers: 2, 3 → max = 3
    assert resolve_posts_per_week(p) == 3


def test_resolve_falls_back_to_seven_when_no_number() -> None:
    """Cadence prose with no parseable number → 7 (status quo)."""
    p = _persona(cadence="posts most days when inspiration strikes")
    assert resolve_posts_per_week(p) == 7


def test_resolve_falls_back_to_seven_when_cadence_empty() -> None:
    # posting_cadence requires min_length=1; use a single space to test edge.
    p = _persona(cadence=" ")
    assert resolve_posts_per_week(p) == 7


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7])
def test_resolve_structured_field_all_valid_values(n: int) -> None:
    p = _persona(cadence="doesn't matter", ppw=n)
    assert resolve_posts_per_week(p) == n
