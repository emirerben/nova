"""measure_caption line layout: keep-together + widow penalties (plan 011, B).

These pin the fit-first contract: with no keep_together pairs and no widow
penalty the wrapping is byte-identical to the pre-feature scoring; when active,
a split that FITS always beats one that overflows (so honoring a pair never
forces an extra shrink), and among fitting splits the layout avoids breaking a
kept pair or stranding a lone short word.
"""

from __future__ import annotations

from app.pipeline.render_geometry import (
    _is_widow,
    _split_breaks_pair,
    _valid_keep_together,
    measure_caption,
)

_KW = dict(
    font_family="Montserrat Bold",
    font_size_px=64,
    width_frac=0.88,
    y_frac=0.705,
    max_lines=2,
)


# ── kill switch: bare call is byte-identical ──────────────────────────────────


def test_no_penalties_is_byte_identical() -> None:
    text = "number one is Messi the goat"
    a = measure_caption(text, **_KW)
    b = measure_caption(text, keep_together=None, penalize_widows=False, **_KW)
    assert a.lines == b.lines
    assert a.font_size_px == b.font_size_px
    assert a.box.as_dict() == b.box.as_dict()


def test_only_stale_pairs_is_byte_identical() -> None:
    text = "number one is Messi the goat"
    baseline = measure_caption(text, **_KW)
    # All pairs out of range / degenerate → filtered to empty → no penalties.
    stale = measure_caption(text, keep_together=[(10, 11), (3, 3), (-1, 2)], **_KW)
    assert stale.lines == baseline.lines


# ── keep_together ─────────────────────────────────────────────────────────────


def test_keep_together_pair_not_split_across_lines() -> None:
    text = "number one is Messi the goat"
    measured = measure_caption(text, keep_together=[(0, 1)], **_KW)
    if len(measured.lines) == 2:
        joined = " | ".join(measured.lines)
        assert "number one" in measured.lines[0] or "number one" in measured.lines[1], joined


def test_overflow_still_wins_over_a_kept_pair() -> None:
    # A pair too wide to share a line with anything must not blank the output —
    # a fitting (necessarily split) layout wins over an overflowing kept one.
    measured = measure_caption(
        "supercalifragilistic expialidocious wordsmith", keep_together=[(0, 1)], **_KW
    )
    assert measured.lines  # produced a real layout, did not crash or blank


# ── widow penalty ─────────────────────────────────────────────────────────────


def test_widow_penalty_avoids_lone_short_word() -> None:
    # "I" would be a one-char widow line; the penalty prefers a balanced split.
    text = "I am the greatest"
    with_widow = measure_caption(text, penalize_widows=True, **_KW)
    for line in with_widow.lines:
        if len(with_widow.lines) == 2:
            assert not (len(line.split()) == 1 and len(line.strip()) <= 3), with_widow.lines


# ── helpers ───────────────────────────────────────────────────────────────────


def test_valid_keep_together_drops_out_of_range_and_degenerate() -> None:
    assert _valid_keep_together([(0, 1), (1, 3)], 4) == [(0, 1), (1, 3)]
    assert _valid_keep_together([(3, 3), (2, 1), (-1, 0), (2, 9)], 4) == []
    assert _valid_keep_together(None, 4) == []


def test_split_breaks_pair_only_inside_the_span() -> None:
    assert _split_breaks_pair(1, [(0, 1)]) is True  # break between w0|w1 splits (0,1)
    assert _split_breaks_pair(2, [(0, 1)]) is False  # break after the pair is fine
    assert _split_breaks_pair(1, [(1, 3)]) is False  # break before the pair is fine
    assert _split_breaks_pair(2, [(1, 3)]) is True  # break inside (1,3)


def test_is_widow_requires_three_plus_words_and_a_short_lone_line() -> None:
    assert _is_widow(("I", "really love it")) is True
    assert _is_widow(("I really", "love it")) is False  # no lone line
    assert _is_widow(("a", "b")) is False  # fewer than 3 words total
    assert _is_widow(("hello", "there friend")) is False  # lone line is not short
