"""Unit tests for app.pipeline.text_reveal.build_cumulative_stages."""

from __future__ import annotations

import pytest

from app.pipeline.text_reveal import (
    LAST_WORD_DWELL_S,
    MIN_RENDERABLE_S,
    CumulativeStage,
    Word,
    build_cumulative_stages,
)


def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start_s=start, end_s=end)


def test_empty_input_returns_empty():
    assert build_cumulative_stages([], line_end_s=5.0) == []


def test_three_words_butted_edges():
    words = [_w("good", 0.0, 1.0), _w("morning", 1.0, 2.0), _w("everyone", 2.0, 3.0)]
    stages = build_cumulative_stages(words, line_end_s=3.0)
    assert len(stages) == 3

    assert stages[0].text == "good"
    assert stages[1].text == "good morning"
    assert stages[2].text == "good morning everyone"

    assert stages[0].pop_animated_suffix == "good"
    assert stages[1].pop_animated_suffix == "morning"
    assert stages[2].pop_animated_suffix == "everyone"

    # Butted: stage[i].end_s == stage[i+1].start_s for non-terminal stages.
    assert stages[0].end_s == pytest.approx(stages[1].start_s)
    assert stages[1].end_s == pytest.approx(stages[2].start_s)

    # Terminal stage extends past line_end_s by dwell.
    assert stages[2].end_s == pytest.approx(3.0 + LAST_WORD_DWELL_S)


def test_single_word_no_expansion():
    stages = build_cumulative_stages([_w("hello", 0.0, 1.0)], line_end_s=1.0)
    assert len(stages) == 1
    assert stages[0].text == "hello"
    assert stages[0].pop_animated_suffix == "hello"
    assert stages[0].start_s == pytest.approx(0.0)
    assert stages[0].end_s == pytest.approx(1.0 + LAST_WORD_DWELL_S)


def test_last_word_dwell_overrides_default():
    stages = build_cumulative_stages([_w("hi", 0.0, 0.5)], line_end_s=0.5, dwell_s=1.0)
    assert stages[0].end_s == pytest.approx(1.5)


def test_middle_stage_dropped_when_below_min_renderable():
    # word B has a natural span of (C.start - B.start) = 0.01s < MIN_RENDERABLE_S.
    words = [
        _w("good", 0.0, 0.5),
        _w("very", 1.0, 1.005),
        _w("morning", 1.01, 2.0),
    ]
    stages = build_cumulative_stages(words, line_end_s=2.0)
    # B should be DROPPED; its text still appears in C's cumulative.
    assert len(stages) == 2
    assert stages[0].text == "good"
    assert stages[1].text == "good very morning"
    assert stages[1].pop_animated_suffix == "morning"
    # A's end_s butts directly to C's start_s (skips dropped B).
    assert stages[0].end_s == pytest.approx(stages[1].start_s)


def test_last_word_always_survives_even_when_short():
    # last word has natural span = line_end_s - last.start_s = 0.01 < MIN_RENDERABLE_S.
    words = [_w("hello", 0.0, 1.0), _w("there", 2.0, 2.0)]
    stages = build_cumulative_stages(words, line_end_s=2.01)
    # Both kept; the terminal stage's end_s is extended by dwell which pads
    # the renderable window even though natural span was below threshold.
    assert len(stages) == 2
    assert stages[1].text == "hello there"
    assert stages[1].end_s == pytest.approx(2.01 + LAST_WORD_DWELL_S)


def test_whitespace_only_word_filtered_from_cumulative():
    # Empty-after-strip words contribute nothing to the cumulative join; caller
    # is responsible for filtering, but the helper is defensive.
    words = [_w("hello", 0.0, 1.0), _w("   ", 1.0, 2.0), _w("world", 2.0, 3.0)]
    stages = build_cumulative_stages(words, line_end_s=3.0)
    # The whitespace stage emits as a stage (it survives the renderable
    # check) but its cumulative text omits the blank word, so its text equals
    # the prior stage's text. Caller should have filtered.
    assert stages[0].text == "hello"
    assert "world" in stages[-1].text


def test_terminal_stage_with_start_past_line_end_is_padded():
    # Pathological input: word starts after line_end_s. Helper still emits a
    # stage with at least MIN_RENDERABLE_S window so caller can validate.
    stages = build_cumulative_stages([_w("late", 5.0, 6.0)], line_end_s=4.0, dwell_s=0.0)
    assert len(stages) == 1
    assert stages[0].end_s - stages[0].start_s == pytest.approx(MIN_RENDERABLE_S)


def test_all_middle_words_dropped_keeps_first_and_last():
    # Tightly-clustered short middle words. Each KEEP decision is based on
    # natural span to NEXT word's start_s, not raw duration. So:
    #   a: span to b = 1.0s → KEEP
    #   b: span to c = 0.01s → DROP
    #   c: span to d = 0.01s → DROP
    #   d: span to e = 0.98s → KEEP (because e starts long after d)
    #   e: terminal → KEEP
    words = [
        _w("a", 0.0, 0.1),
        _w("b", 1.0, 1.005),
        _w("c", 1.01, 1.015),
        _w("d", 1.02, 1.025),
        _w("e", 2.0, 2.5),
    ]
    stages = build_cumulative_stages(words, line_end_s=2.5)
    assert len(stages) == 3
    assert [s.pop_animated_suffix for s in stages] == ["a", "d", "e"]
    assert stages[0].text == "a"
    assert stages[1].text == "a b c d"  # cumulative carries DROPPED b, c
    assert stages[2].text == "a b c d e"
    # Butted edges across the dropped middle: a.end == d.start, d.end == e.start.
    assert stages[0].end_s == pytest.approx(stages[1].start_s)
    assert stages[1].end_s == pytest.approx(stages[2].start_s)


def test_returns_cumulative_stage_dataclass():
    stages = build_cumulative_stages([_w("x", 0.0, 1.0)], line_end_s=1.0)
    assert isinstance(stages[0], CumulativeStage)


# --- butt_join_cumulative_phrases -------------------------------------------

from app.pipeline.text_reveal import (  # noqa: E402
    butt_join_cumulative_phrases,
    group_phrase_index_blocks,
)


def _ov(text: str, start_s: float, end_s: float, **extra) -> dict:
    return {"sample_text": text, "start_s": start_s, "end_s": end_s, **extra}


def test_buttjoin_closes_intra_phrase_gaps_from_prod_recipe():
    # The exact gappy slice from prod template 89cde014 (job 8eaee104): each
    # stage is start+0.4, but starts are spaced wider, leaving blank holes.
    ovs = [
        _ov("if", 2.057, 2.457),
        _ov("if you", 2.457, 2.857),
        _ov("if you put", 3.257, 3.657),  # 0.4s gap before this
        _ov("if you put in", 4.057, 4.857),  # 0.4s gap before this (terminal)
    ]
    extended = butt_join_cumulative_phrases(ovs)
    assert extended == 2
    # Every non-terminal end now equals the next start: continuous on screen.
    assert ovs[0]["end_s"] == pytest.approx(ovs[1]["start_s"])
    assert ovs[1]["end_s"] == pytest.approx(ovs[2]["start_s"])
    assert ovs[2]["end_s"] == pytest.approx(ovs[3]["start_s"])
    # Terminal dwell untouched; no start moved.
    assert ovs[3]["end_s"] == pytest.approx(4.857)
    assert [o["start_s"] for o in ovs] == pytest.approx([2.057, 2.457, 3.257, 4.057])


def test_buttjoin_never_crosses_phrase_boundary():
    # Two distinct phrases — the gap between them is intentional clear+dwell.
    ovs = [
        _ov("Luck", 5.288, 5.688),
        _ov("Luck is", 6.088, 6.488),  # same phrase: gap closed
        _ov("if", 9.000, 9.400),  # NEW phrase: gap before it preserved
        _ov("if you", 9.400, 9.800),
    ]
    butt_join_cumulative_phrases(ovs)
    assert ovs[0]["end_s"] == pytest.approx(ovs[1]["start_s"])  # closed
    # "Luck is" (terminal of phrase 1) must NOT stretch to "if" (phrase 2).
    assert ovs[1]["end_s"] == pytest.approx(6.488)
    assert ovs[2]["start_s"] == pytest.approx(9.000)  # phrase 2 start untouched


def test_buttjoin_idempotent_on_already_butted():
    ovs = [_ov("a", 0.0, 0.4), _ov("a b", 0.4, 0.8), _ov("a b c", 0.8, 1.6)]
    assert butt_join_cumulative_phrases(ovs) == 0
    assert butt_join_cumulative_phrases(ovs) == 0


def test_buttjoin_pct_timed_phrase():
    # Agentic pct overlays: butt end_pct to the next start_pct.
    ovs = [
        _ov("x", 0.0, 0.1, start_pct=0.0, end_pct=0.1),
        _ov("x y", 0.1, 0.2, start_pct=0.3, end_pct=0.4),  # gap in pct space
    ]
    assert butt_join_cumulative_phrases(ovs) == 1
    assert ovs[0]["end_pct"] == pytest.approx(0.3)


def test_buttjoin_pct_idempotent_when_already_butted():
    ovs = [
        _ov("x", 0.0, 0.1, start_pct=0.0, end_pct=0.3),
        _ov("x y", 0.1, 0.2, start_pct=0.3, end_pct=0.4),
    ]
    assert butt_join_cumulative_phrases(ovs) == 0


def test_buttjoin_pct_cur_with_seconds_only_next_does_not_corrupt_end_s():
    # cur renders via pct (both pct fields present); nxt is a partially-retimed
    # sibling with NO start_pct. We must NOT extend cur.end_s (which the renderer
    # ignores for a pct overlay) and must NOT claim success.
    ovs = [
        _ov("x", 0.0, 0.1, start_pct=0.0, end_pct=0.1),
        _ov("x y", 0.5, 0.9),  # seconds only, no start_pct
    ]
    assert butt_join_cumulative_phrases(ovs) == 0
    assert ovs[0]["end_s"] == pytest.approx(0.1)  # untouched
    assert ovs[0]["end_pct"] == pytest.approx(0.1)  # untouched


def test_buttjoin_respects_end_override():
    ovs = [
        _ov("x", 0.0, 0.4, end_s_override=0.4),
        _ov("x y", 1.0, 1.4, start_s_override=1.0),
    ]
    assert butt_join_cumulative_phrases(ovs) == 1
    assert ovs[0]["end_s_override"] == pytest.approx(1.0)


def test_buttjoin_override_idempotent_when_already_butted():
    ovs = [
        _ov("x", 0.0, 1.0, end_s_override=1.0),
        _ov("x y", 1.0, 1.4, start_s_override=1.0),
    ]
    assert butt_join_cumulative_phrases(ovs) == 0


def test_buttjoin_skips_cross_anchor_prefix_collision():
    # Classic false-match: "Go" (top) then "Going home" (bottom) — text-prefix
    # match but DIFFERENT anchors. Must NOT join (would flash both at the seam).
    ovs = [
        _ov("Go", 0.0, 0.4, position_x_frac=0.5, position_y_frac=0.20),
        _ov("Going home", 1.0, 1.4, position_x_frac=0.5, position_y_frac=0.80),
    ]
    assert butt_join_cumulative_phrases(ovs) == 0
    assert ovs[0]["end_s"] == pytest.approx(0.4)  # title left as authored


def test_buttjoin_joins_same_anchor_reveal():
    # Same prefix relationship, SAME anchor → genuine reveal → join.
    ovs = [
        _ov("Go", 0.0, 0.4, position_x_frac=0.05, position_y_frac=0.44),
        _ov("Going home", 1.0, 1.4, position_x_frac=0.05, position_y_frac=0.44),
    ]
    assert butt_join_cumulative_phrases(ovs) == 1
    assert ovs[0]["end_s"] == pytest.approx(1.0)


def test_buttjoin_handles_non_dict_entries():
    ovs = [_ov("a", 0.0, 0.4), "not-a-dict", _ov("a b", 1.0, 1.4)]
    # The non-dict breaks the run into singletons → nothing to join, no crash.
    assert butt_join_cumulative_phrases(ovs) == 0


def test_buttjoin_singleton_and_empty_noop():
    assert butt_join_cumulative_phrases([]) == 0
    assert butt_join_cumulative_phrases([_ov("solo", 0.0, 1.0)]) == 0


def test_group_phrase_index_blocks_moved_here():
    ovs = [_ov("a", 0, 1), _ov("a b", 1, 2), _ov("a b c", 2, 3), _ov("new", 3, 4)]
    assert group_phrase_index_blocks(ovs) == [[0, 1, 2], [3]]
