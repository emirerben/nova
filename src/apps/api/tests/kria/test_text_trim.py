"""KRI-552: text shortened for a creator-facing question is never cut mid-word."""

from __future__ import annotations

import pytest

from app.kria.text_trim import trim_at_word


def test_the_prod_label_keeps_whole_words() -> None:
    label = "the part where I restart the kilometer thirty sentence"

    # The old 40-character slice ended "...restart the kilometer t".
    assert label[:40] == "the part where I restart the kilometer t"
    assert trim_at_word(label, 40) == "the part where I restart the kilometer…"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short enough", 40, "short enough"),
        ("exactly ten", 11, "exactly ten"),
        ("one two three", 9, "one two…"),
        ("one two, three", 10, "one two…"),  # no dangling comma before the ellipsis
        ("supercalifragilistic", 8, "superca…"),  # one word with nowhere to break
        ("klibi çamaşır sepetinde buldum", 20, "klibi çamaşır…"),
        ("line one\nline two is long", 14, "line one\nline…"),
    ],
)
def test_trim_cases(text: str, limit: int, expected: str) -> None:
    out = trim_at_word(text, limit)

    assert out == expected
    assert len(out) <= limit
