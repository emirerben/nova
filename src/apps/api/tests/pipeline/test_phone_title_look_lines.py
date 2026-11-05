"""Qendresa item 20: which typed titles are her stacked lines (F1, F2 of the failure list).

She types line breaks as " / " ("5 PROJECTS / the AI builder community / ..."). A slash
that is part of ordinary text ("24/7", "A/B testing", "and/or") or a one-letter part
("A / B") is not a line break. Her words, casing and punctuation are kept exactly.
"""

from __future__ import annotations

import pytest
from app.pipeline.phone_title_look import split_title_lines


@pytest.mark.parametrize(
    ("title", "lines"),
    [
        (
            "5 PROJECTS / the AI builder community / can't stop talking about!!!!",
            ["5 PROJECTS", "the AI builder community", "can't stop talking about!!!!"],
        ),
        (
            "lets build / an agent / that reads / an APP",
            ["lets build", "an agent", "that reads", "an APP"],
        ),
        ("5 projects\nto build this weekend", ["5 projects", "to build this weekend"]),
        ("Before / After", ["Before", "After"]),
        ("  spaced  /  out  ", ["spaced", "out"]),
    ],
)
def test_her_line_breaks_are_kept_in_her_order_and_casing(title, lines):
    assert split_title_lines(title) == lines


@pytest.mark.parametrize(
    "title",
    [
        None,
        "",
        "lets build an agent that reads an APP before I trust it!!",
        "open 24/7 now",
        "A/B testing my hooks",
        "and/or",
        "A / B testing",  # a one-letter part is a slash in her text, not a line
        "x / ",
        "one / two / three / four / five / six",  # more lines than a hook title holds
    ],
)
def test_ordinary_slashes_and_single_lines_are_not_split(title):
    assert split_title_lines(title) is None
