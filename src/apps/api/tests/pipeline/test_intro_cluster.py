"""Tests for the word-cluster intro layout engine (app/pipeline/intro_cluster.py).

The engine is the geometry source of truth for cluster intros: determinism and
the no-clip invariant are the contract the renderer relies on (no `_shrink_to_fit`
rescue pass exists across blocks — the engine must emit sizes/positions that fit).

Layout tests need skia for real glyph measurement (same dependency story as
test_text_overlay_skia.py); pure-logic tests (roles, grouping, fallbacks) don't.
"""

from __future__ import annotations

from itertools import combinations
from unittest import mock

import skia  # noqa: F401 — layout tests require the real measurement backend

from app.pipeline.intro_cluster import (
    _EDGE_MARGIN_FRAC,
    _Y_MAX,
    _Y_MIN,
    MAX_WORDS,
    MIN_WORDS,
    ROLE_CLOSER,
    ROLE_CONNECTOR,
    ROLE_HERO,
    compute_cluster_blocks,
    derive_word_roles,
)
from app.pipeline.text_overlay_skia import CANVAS_W, _typeface_for_overlay

_REFERENCE_TEXT = "what's your favorite place?"


def _compute(text: str, **kw):
    defaults = dict(word_roles=None, base_size_px=60, font_family=None, reveal_window_s=3.0)
    defaults.update(kw)
    return compute_cluster_blocks(text, **defaults)


# -- derive_word_roles ---------------------------------------------------------


def test_roles_stopwords_become_connectors_and_punctuated_tail_closes():
    roles = derive_word_roles(_REFERENCE_TEXT.split())
    assert roles[0] == ROLE_CONNECTOR  # what's
    assert roles[2] == ROLE_HERO  # favorite
    assert roles[3] == ROLE_CLOSER  # place?


def test_roles_highlight_word_is_always_hero():
    roles = derive_word_roles(["your", "favorite", "place"], highlight_word="your")
    assert roles[0] == ROLE_HERO


def test_roles_all_stopword_text_promotes_longest_to_hero():
    roles = derive_word_roles(["this", "and", "that"])
    assert ROLE_HERO in roles


def test_roles_signal_free_text_still_gets_contrast():
    # The prod flat-stack regression: a user-override hook with no stopword,
    # punctuation, or highlight signal (typical for Turkish) must NOT come back
    # all-hero — the heuristic demotes the edges so the cluster keeps its
    # size hierarchy.
    roles = derive_word_roles("Lutfen calis haydi inaniyorum haydi".split())
    assert roles == [ROLE_CONNECTOR, ROLE_HERO, ROLE_HERO, ROLE_HERO, ROLE_CLOSER]
    # 4 words: closer demotion only (heroes stay the dramatic majority).
    assert derive_word_roles("london mornings hit different".split()) == [
        ROLE_HERO,
        ROLE_HERO,
        ROLE_HERO,
        ROLE_CLOSER,
    ]
    # Highlight on the final word blocks the closer demotion — highlight is hero.
    roles = derive_word_roles(["calis", "haydi", "inaniyorum", "lutfen"], highlight_word="lutfen")
    assert roles[-1] == ROLE_HERO


# -- engine fallbacks ----------------------------------------------------------


def test_word_count_outside_range_returns_none():
    assert _compute("hello world") is None  # 2 < MIN_WORDS
    assert _compute("one two three four five six seven") is None  # 7 > MAX_WORDS
    assert MIN_WORDS == 3 and MAX_WORDS == 6  # doc-locked range


def test_invalid_agent_roles_fall_back_to_heuristic():
    # Misaligned length and unknown vocab both fall back instead of crashing.
    assert _compute(_REFERENCE_TEXT, word_roles=["hero"]) is not None
    assert _compute(_REFERENCE_TEXT, word_roles=["big", "big", "big", "big"]) is not None


# -- determinism ---------------------------------------------------------------


def test_layout_is_deterministic():
    a = _compute(_REFERENCE_TEXT, font_family="Playfair Display")
    b = _compute(_REFERENCE_TEXT, font_family="Playfair Display")
    assert a == b


# -- no-clip invariant ---------------------------------------------------------


def _assert_no_clip(blocks):
    """Re-measure every block at its emitted px/font and assert it stays inside
    the frame margins — the invariant the engine guarantees by construction."""
    for blk in blocks:
        tf = _typeface_for_overlay(
            {"font_family": blk["font_family"]} if blk["font_family"] else {}
        )
        font = skia.Font(tf, blk["text_size_px"])
        font.setSubpixel(True)
        half_w = font.measureText(blk["text"]) / CANVAS_W / 2
        x = blk["position_x_frac"]
        assert x - half_w >= _EDGE_MARGIN_FRAC - 1e-6, f"left clip: {blk['text']}"
        assert x + half_w <= 1 - _EDGE_MARGIN_FRAC + 1e-6, f"right clip: {blk['text']}"
        assert _Y_MIN - 1e-6 <= blk["position_y_frac"] <= _Y_MAX + 1e-6


def _bbox(blk):
    tf = _typeface_for_overlay({"font_family": blk["font_family"]} if blk["font_family"] else {})
    font = skia.Font(tf, blk["text_size_px"])
    font.setSubpixel(True)
    metrics = font.getMetrics()
    half_w = font.measureText(blk["text"]) / CANVAS_W / 2
    half_h = (metrics.fDescent - metrics.fAscent) / 1920 / 2
    return {
        "text": blk["text"],
        "left": blk["position_x_frac"] - half_w,
        "right": blk["position_x_frac"] + half_w,
        "top": blk["position_y_frac"] - half_h,
        "bottom": blk["position_y_frac"] + half_h,
    }


def _assert_no_overlap(blocks):
    boxes = [_bbox(blk) for blk in blocks]
    for a, b in combinations(boxes, 2):
        overlap_x = a["left"] < b["right"] - 1e-6 and b["left"] < a["right"] - 1e-6
        overlap_y = a["top"] < b["bottom"] - 1e-6 and b["top"] < a["bottom"] - 1e-6
        assert not (overlap_x and overlap_y), f"collision: {a['text']} vs {b['text']}"


def test_no_clip_across_text_shapes():
    cases = [
        (_REFERENCE_TEXT, None),
        ("the days we never planned", "Playfair Display"),
        # longish hero word at max base size — exercises the atomic-shrink path
        ("wandering around old istanbul", None),
        # Turkish diacritics
        ("en sevdiğin yer neresi?", "Playfair Display"),
        ("one two three four five six", None),
    ]
    for text, family in cases:
        blocks = _compute(text, font_family=family, base_size_px=80)
        assert blocks, f"engine declined {text!r}"
        _assert_no_clip(blocks)
        _assert_no_overlap(blocks)


def test_connector_never_collides_with_first_hero_regression():
    # Prod proof still regression: "this" visually touched "bridge". Connectors
    # must sit beside their anchor hero with an actual measured gap.
    blocks = _compute(
        "this bridge sunset",
        word_roles=[ROLE_CONNECTOR, ROLE_HERO, ROLE_HERO],
        font_family="Playfair Display",
        base_size_px=60,
    )
    assert blocks is not None
    _assert_no_clip(blocks)
    _assert_no_overlap(blocks)
    by_text = {b["text"]: b for b in blocks}
    assert abs(by_text["this"]["position_y_frac"] - by_text["bridge"]["position_y_frac"]) <= 0.015


def test_atomic_shrink_preserves_size_hierarchy():
    # "wandering" at base 80 (hero 208px) overflows the usable width →
    # cluster-atomic shrink. Hero must shrink BELOW the unshrunk size yet stay
    # larger than the connector. Roles pinned so the test controls block widths.
    blocks = _compute(
        "wandering through golden istanbul",
        word_roles=["hero", "connector", "connector", "hero"],
        base_size_px=80,
    )
    assert blocks is not None
    by_role: dict[str, int] = {}
    for blk in blocks:
        by_role.setdefault(blk["role"], blk["text_size_px"])
    assert by_role[ROLE_HERO] < 208  # shrink actually happened
    assert by_role[ROLE_HERO] > by_role[ROLE_CONNECTOR]


def test_cluster_roles_and_shrink_events_have_documented_payloads():
    with mock.patch("app.services.pipeline_trace.record_pipeline_event") as record:
        blocks = _compute(
            "wandering through golden istanbul",
            word_roles=["hero", "connector", "connector", "hero"],
            base_size_px=80,
        )
    assert blocks is not None
    events = [(c.args[1], c.args[2]) for c in record.call_args_list]

    roles_payload = next(payload for event, payload in events if event == "cluster_roles_derived")
    assert roles_payload["words"] == ["wandering", "through", "golden", "istanbul"]
    assert roles_payload["input_roles"] == ["hero", "connector", "connector", "hero"]
    assert roles_payload["effective_roles"] == ["hero", "connector", "connector", "hero"]
    assert roles_payload["role_source"] == "agent"
    assert set(roles_payload["guarantees"]) == {
        "invalid_roles_rederived",
        "closer_final_enforced",
        "hero_present_enforced",
        "signal_free_contrast_enforced",
        "all_hero_demoted_to_closer",
    }

    shrink_payload = next(payload for event, payload in events if event == "cluster_shrink_applied")
    assert shrink_payload["text"] == "wandering through golden istanbul"
    assert shrink_payload["base_size_px"] == 80
    assert 0.55 <= shrink_payload["scale"] < 1.0
    assert shrink_payload["min_scale"] == 0.55
    assert shrink_payload["widest_block_frac"] <= shrink_payload["usable_width_frac"]
    assert shrink_payload["block_count"] == len(blocks)


def test_intro_layout_selected_event_has_documented_payload():
    from app.pipeline.generative_overlays import build_persistent_intro_overlays

    with mock.patch("app.services.pipeline_trace.record_pipeline_event") as record:
        overlays = build_persistent_intro_overlays(
            text="what's your favorite place?",
            effect="static",
            reveal_window_s=3.0,
            layout="cluster",
            word_roles=[ROLE_CONNECTOR, ROLE_HERO, ROLE_HERO, ROLE_CLOSER],
            font_family="Playfair Display",
            text_size_px=60,
        )
    assert len(overlays) > 2
    layout_payload = next(
        c.args[2] for c in record.call_args_list if c.args[1] == "intro_layout_selected"
    )
    assert layout_payload == {
        "requested_layout": "cluster",
        "layout_source": "model",
        "selected_layout": "cluster",
        "reason": "agent_pick",
        "text": "what's your favorite place?",
        "word_count": 4,
        "has_word_roles": True,
        "fallback": False,
    }


def test_cluster_roles_event_reports_per_word_roles_before_grouping():
    with mock.patch("app.services.pipeline_trace.record_pipeline_event") as record:
        blocks = _compute("one two three four five six", word_roles=[ROLE_HERO] * 6)
    assert blocks is not None
    roles_payload = next(
        c.args[2] for c in record.call_args_list if c.args[1] == "cluster_roles_derived"
    )
    assert roles_payload["effective_roles"] == [ROLE_HERO] * 6
    assert roles_payload["guarantees"]["all_hero_demoted_to_closer"] is True


def test_cluster_event_emission_outside_trace_context_does_not_raise():
    assert _compute(
        "wandering through golden istanbul",
        word_roles=["hero", "connector", "connector", "hero"],
        base_size_px=80,
    )


def test_unfittable_long_words_decline_to_linear():
    # A hero word so long the cluster would shrink past the readability floor
    # → engine declines (caller falls back to the proven linear intro).
    assert (
        _compute("muvaffakiyetsizlestiricilestiriveremeyebileceklerimizdenmissinizcesine kal orada")
        is None
    )


# -- structure / timing --------------------------------------------------------


def test_blocks_cover_all_words_in_order():
    blocks = _compute(_REFERENCE_TEXT)
    assert " ".join(b["text"] for b in blocks) == _REFERENCE_TEXT


def test_stagger_monotonic_and_inside_reveal_window():
    blocks = _compute(_REFERENCE_TEXT, reveal_window_s=3.0)
    starts = [b["start_offset_s"] for b in blocks]
    assert starts == sorted(starts)
    assert starts[0] == 0.0
    for b in blocks:
        assert b["start_offset_s"] < 3.0
        assert b["start_offset_s"] + b["reveal_s"] <= 3.0 + 0.31  # _MIN_REVEAL_S slack


def test_short_reveal_window_clamps_starts():
    blocks = _compute(_REFERENCE_TEXT, reveal_window_s=1.0)
    assert blocks is not None
    for b in blocks:
        assert b["start_offset_s"] <= 0.5


def test_hero_lines_stack_downward_with_distinct_positions():
    blocks = _compute("the days we never planned", font_family="Playfair Display")
    heroes = [b for b in blocks if b["role"] == ROLE_HERO]
    assert len(heroes) >= 2
    ys = [h["position_y_frac"] for h in heroes]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)
    xs = {h["position_x_frac"] for h in heroes}
    assert len(xs) >= 2  # alternating jitter — not a straight centered column


def test_connector_uses_regular_weight_of_hero_face():
    blocks = _compute(_REFERENCE_TEXT, font_family="Playfair Display")
    connector = next(b for b in blocks if b["role"] == ROLE_CONNECTOR)
    assert connector["font_family"] == "Playfair Display Regular"
    hero = next(b for b in blocks if b["role"] == ROLE_HERO)
    assert hero["font_family"] == "Playfair Display"


def test_repeated_hero_words_keep_screen_order():
    # "go go go go fast" — duplicate hero dicts compare equal; the hero-cap fold
    # must locate the fold point by IDENTITY or the on-screen word order
    # silently scrambles.
    blocks = _compute("go go go go fast", word_roles=["hero", "hero", "hero", "hero", "hero"])
    assert blocks is not None
    assert " ".join(b["text"] for b in blocks) == "go go go go fast"


def test_flat_stack_regression_cluster_always_has_size_contrast():
    # Prod regression (plan item 7da07d29, 2026-06-11): an all-hero role set —
    # heuristic-derived OR agent-annotated — used to render every block at one
    # size after the surplus-hero merge forced an atomic shrink. The result was
    # indistinguishable from linear text. A cluster MUST carry ≥2 distinct
    # sizes, and heroes must not have been crushed by the fold.
    cases = [
        ("Lutfen calis haydi inaniyorum haydi", None, 54),  # the prod text, heuristic path
        ("go go go go fast", ["hero"] * 5, 60),  # agent-annotated all-hero
        ("dream eat repeat", ["hero"] * 3, 60),  # minimal all-hero
    ]
    for text, roles, base in cases:
        blocks = _compute(text, word_roles=roles, base_size_px=base)
        assert blocks, f"engine declined {text!r}"
        sizes = {b["text_size_px"] for b in blocks}
        assert len(sizes) >= 2, f"flat cluster for {text!r}: {sizes}"
        hero_px = max(b["text_size_px"] for b in blocks if b["role"] == ROLE_HERO)
        assert hero_px == round(base * 2.6), f"hero crushed for {text!r}: {hero_px}"


def test_surplus_heroes_fold_into_closer_not_hero_line():
    # 6 hero words: the 3 surplus words become ONE closer-sized tail block, in
    # reading order — never a triple-wide hero line that forces a shrink.
    blocks = _compute("one two three four five six", word_roles=["hero"] * 6)
    assert blocks is not None
    assert " ".join(b["text"] for b in blocks) == "one two three four five six"
    closers = [b for b in blocks if b["role"] == ROLE_CLOSER]
    assert len(closers) == 1 and closers[-1]["text"] == "four five six"
    assert len([b for b in blocks if b["role"] == ROLE_HERO]) == 3


def test_mid_text_closer_annotation_falls_back_to_heuristic():
    # A closer anywhere but the final word breaks the geometry's shape
    # assumptions (blocks would stack) — the engine must re-derive roles.
    blocks = _compute(
        "home is where we wander",
        word_roles=["hero", "closer", "connector", "connector", "hero"],
    )
    assert blocks is not None
    _assert_no_clip(blocks)
    positions = [(b["position_x_frac"], b["position_y_frac"]) for b in blocks]
    assert len(set(positions)) == len(positions)  # no two blocks stacked


def test_emitted_px_never_below_renderer_floor():
    # The renderer raises anything under 24px back to 24 at draw time; the
    # engine must never emit a px it didn't actually measure at.
    blocks = _compute("the quiet side of tokyo", base_size_px=40)
    assert blocks is not None
    assert all(b["text_size_px"] >= 24 for b in blocks)
