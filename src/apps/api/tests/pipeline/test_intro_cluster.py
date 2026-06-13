"""Tests for the word-cluster intro layout engine (app/pipeline/intro_cluster.py).

The engine is the geometry source of truth for cluster intros: determinism and
the no-clip invariant are the contract the renderer relies on (no `_shrink_to_fit`
rescue pass exists across blocks — the engine must emit sizes/positions that fit).

Layout tests need skia for real glyph measurement (same dependency story as
test_text_overlay_skia.py); pure-logic tests (roles, grouping, fallbacks) don't.
"""

from __future__ import annotations

from unittest import mock

import skia  # noqa: F401 — layout tests require the real measurement backend

from app.pipeline.intro_cluster import (
    _CLUSTER_CENTER_Y,
    _EDGE_MARGIN_FRAC,
    _Y_MAX,
    _Y_MIN,
    EDITORIAL_STYLE,
    MAX_WORDS,
    MIN_WORDS,
    ROLE_CLOSER,
    ROLE_CONNECTOR,
    ROLE_HERO,
    compute_cluster_blocks,
    derive_word_roles,
    normalize_typography,
    scene_center_y,
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


# -- editorial style (style=EDITORIAL_STYLE) ------------------------------------


def _compute_styled(text: str, **kw):
    defaults = dict(
        word_roles=None,
        base_size_px=60,
        font_family=None,
        reveal_window_s=3.0,
        style=EDITORIAL_STYLE,
    )
    defaults.update(kw)
    return compute_cluster_blocks(text, **defaults)


def test_editorial_style_documents_its_contract_keys():
    # EDITORIAL_STYLE is the single source of truth (decision D13) — the
    # sequence emitter and renderer tuning read these keys; renaming one is an
    # API break for them.
    assert {
        "hero_font",
        "body_font",
        "accent_font",
        "hero_ratio",
        "connector_ratio",
        "closer_ratio",
        "script_min_px",
        "shadow",
        "cascade_x_start",
        "cascade_x_step",
        "cascade_x_jitter",
        "cascade_y_step_ratio",
        "scene_center_ys",
        "scene_shift_margin",
        "min_words",
        "max_blocks",
    } <= set(EDITORIAL_STYLE)
    assert EDITORIAL_STYLE["hero_font"] == "Great Vibes"
    assert EDITORIAL_STYLE["body_font"] == "Playfair Display Regular"
    assert EDITORIAL_STYLE["accent_font"] == "Playfair Display Italic"
    assert {"alpha", "blur", "dy"} <= set(EDITORIAL_STYLE["shadow"])


def test_style_none_is_byte_identical_legacy():
    # The kill-switch contract: omitting style and passing style=None are the
    # SAME code path, and legacy numerology (hero 2.6x) is untouched.
    a = _compute(_REFERENCE_TEXT, font_family="Playfair Display")
    b = compute_cluster_blocks(
        _REFERENCE_TEXT,
        word_roles=None,
        base_size_px=60,
        font_family="Playfair Display",
        reveal_window_s=3.0,
        style=None,
    )
    assert a == b
    hero = next(blk for blk in a if blk["role"] == ROLE_HERO)
    assert hero["text_size_px"] == round(60 * 2.6)


def test_editorial_faces_come_from_style_not_font_family_arg():
    # style owns the faces: hero → script, others → body/accent alternation,
    # even when the caller passes a font_family (legacy behavior would have
    # heroes use it).
    blocks = _compute_styled(_REFERENCE_TEXT, font_family="Montserrat")
    assert blocks is not None
    for blk in blocks:
        if blk["role"] == ROLE_HERO:
            assert blk["font_family"] == "Great Vibes"
        else:
            assert blk["font_family"] in (
                "Playfair Display Regular",
                "Playfair Display Italic",
            )
    assert not any(blk["font_family"] == "Montserrat" for blk in blocks)


def test_editorial_size_ratios_restrained():
    blocks = _compute_styled(_REFERENCE_TEXT, base_size_px=80)
    assert blocks is not None
    by_role = {blk["role"]: blk["text_size_px"] for blk in blocks}
    assert by_role[ROLE_HERO] == round(80 * 1.7)
    assert by_role[ROLE_CONNECTOR] == 80
    assert by_role[ROLE_CLOSER] == round(80 * 1.25)


def test_editorial_script_blocks_floor_at_script_min_px():
    blocks = _compute_styled("the quiet side", base_size_px=30)
    assert blocks is not None
    hero = next(blk for blk in blocks if blk["role"] == ROLE_HERO)
    assert hero["text_size_px"] >= EDITORIAL_STYLE["script_min_px"]


def test_editorial_cascade_reading_order_is_spatial_order():
    # Diagonal cascade: y strictly increases block-to-block IN TEXT ORDER and
    # x staggers left → right from ~0.40.
    blocks = _compute_styled(_REFERENCE_TEXT)
    assert blocks is not None
    assert " ".join(b["text"] for b in blocks) == normalize_typography(_REFERENCE_TEXT)
    ys = [b["position_y_frac"] for b in blocks]
    assert all(y2 > y1 for y1, y2 in zip(ys, ys[1:])), f"y not strictly increasing: {ys}"
    xs = [b["position_x_frac"] for b in blocks]
    assert all(x2 > x1 for x1, x2 in zip(xs, xs[1:])), f"x not staggering right: {xs}"
    assert abs(xs[0] - EDITORIAL_STYLE["cascade_x_start"]) < 0.02


def test_editorial_trailing_connector_anchors_after_preceding_block():
    # The legacy connector-anchor bug: a connector with no FOLLOWING hero
    # snaps back up beside hero 0 (`next(..., hero_idx[0])`). In styled mode
    # reading order is sacred — the trailing connector sits BELOW the hero.
    blocks = _compute_styled("chase it now", word_roles=["hero", "connector", "connector"])
    assert blocks is not None
    assert [b["role"] for b in blocks] == [ROLE_HERO, ROLE_CONNECTOR]
    assert blocks[1]["position_y_frac"] > blocks[0]["position_y_frac"]


def test_editorial_one_emphasis_group_only():
    # Two separated hero runs: only the FIRST renders in script; the later
    # hero demotes (reading order preserved, no second script block).
    blocks = _compute_styled("dream big tonight", word_roles=["hero", "connector", "hero"])
    assert blocks is not None
    script_blocks = [b for b in blocks if b["font_family"] == "Great Vibes"]
    assert len(script_blocks) == 1
    assert script_blocks[0]["text"] == "dream"
    assert " ".join(b["text"] for b in blocks) == "dream big tonight"


def test_editorial_adjacent_hero_words_merge_into_one_script_block():
    # Unlike legacy (one stacked line per hero word), the emphasis group is
    # ONE block.
    blocks = _compute_styled("the days we lost", word_roles=["connector", "hero", "hero", "hero"])
    assert blocks is not None
    heroes = [b for b in blocks if b["role"] == ROLE_HERO]
    assert len(heroes) == 1
    assert heroes[0]["text"] == "days we lost"
    assert heroes[0]["font_family"] == "Great Vibes"


def test_editorial_scene_caps_at_three_blocks():
    blocks = _compute_styled(
        "one two three four five six",
        word_roles=["connector", "hero", "connector", "closer", "connector", "closer"],
    )
    assert blocks is not None
    assert len(blocks) <= EDITORIAL_STYLE["max_blocks"]
    assert " ".join(b["text"] for b in blocks) == "one two three four five six"


def test_editorial_min_words_allows_single_word_scene():
    blocks = _compute_styled("Rome.")
    assert blocks is not None
    assert len(blocks) == 1
    assert blocks[0]["role"] == ROLE_HERO
    assert blocks[0]["font_family"] == "Great Vibes"
    # Legacy keeps its 3-word floor — the constant is untouched.
    assert _compute("Rome.") is None


def test_editorial_curly_punctuation_applied_per_block():
    blocks = _compute_styled('it\'s "gold" here...', word_roles=None)
    assert blocks is not None
    joined = " ".join(b["text"] for b in blocks)
    assert "’" in joined and "“" in joined and "”" in joined and "…" in joined
    assert "'" not in joined and '"' not in joined
    # Legacy text is NEVER normalized.
    legacy = _compute("it's your favorite place?")
    assert "'" in " ".join(b["text"] for b in legacy)


def test_normalize_typography_mappings():
    assert normalize_typography("it's") == "it’s"
    assert normalize_typography('she said "go" and "stay"') == "she said “go” and “stay”"
    assert normalize_typography("wait...") == "wait…"
    # Idempotent — already-curly text passes through.
    assert normalize_typography("it’s “gold” …") == "it’s “gold” …"


def test_scene_center_y_cycles_deterministically():
    assert scene_center_y(0) == 0.42
    assert scene_center_y(1) == 0.46
    assert scene_center_y(2) == 0.44
    assert scene_center_y(3) == 0.42  # cycle wraps
    # Every center stays within the shift margin of the layout's default
    # center — the contract that makes caller-side scene shifting clip-safe.
    margin = EDITORIAL_STYLE["scene_shift_margin"]
    for i in range(6):
        assert abs(scene_center_y(i) - _CLUSTER_CENTER_Y) <= margin + 1e-9


def test_editorial_glyph_gate_falls_back_hero_block_to_body_font():
    # 'Δ' has a glyph in Playfair Display Regular but NOT in Great Vibes — the
    # hero block must fall back to the body face instead of rendering tofu.
    blocks = _compute_styled("the Δ sign")
    assert blocks is not None
    hero = next(b for b in blocks if b["role"] == ROLE_HERO)
    assert "Δ" in hero["text"]
    assert hero["font_family"] == "Playfair Display Regular"


def test_editorial_glyph_gate_declines_when_body_font_also_missing():
    # CJK is missing from BOTH faces → the scene declines (caller falls back
    # to the static/linear treatment) instead of rendering tofu.
    assert _compute_styled("the 你好 moment") is None


def test_editorial_no_clip_invariant_holds():
    cases = [
        _REFERENCE_TEXT,
        "the days we never planned",
        "wandering around old istanbul",
        "en sevdiğin yer neresi?",
        "one two three four five six",
        "Rome.",
    ]
    for text in cases:
        blocks = _compute_styled(text, base_size_px=80)
        assert blocks, f"styled engine declined {text!r}"
        _assert_no_clip(blocks)
        # Tightened band: block EDGES clear the scene-shift margin so callers
        # can shift the whole scene by scene_center_y deltas without clipping.
        margin = EDITORIAL_STYLE["scene_shift_margin"]
        for blk in blocks:
            tf = _typeface_for_overlay({"font_family": blk["font_family"]})
            font = skia.Font(tf, blk["text_size_px"])
            font.setSubpixel(True)
            metrics = font.getMetrics()
            half_h = (metrics.fDescent - metrics.fAscent) / 1920 / 2
            assert blk["position_y_frac"] - half_h >= _Y_MIN + margin - 1e-6
            assert blk["position_y_frac"] + half_h <= _Y_MAX - margin + 1e-6


def test_editorial_unfittable_text_declines_same_contract_as_legacy():
    assert (
        _compute_styled(
            "muvaffakiyetsizlestiricilestiriveremeyebileceklerimizdenmissinizcesine kal orada"
        )
        is None
    )


def test_editorial_blocks_keep_exact_legacy_keys():
    # Downstream emission depends on this exact key set — adding or renaming
    # keys silently breaks the overlay emitter.
    blocks = _compute_styled(_REFERENCE_TEXT)
    assert blocks is not None
    for blk in blocks:
        assert set(blk) == {
            "text",
            "role",
            "text_size_px",
            "font_family",
            "position_x_frac",
            "position_y_frac",
            "start_offset_s",
            "reveal_s",
        }


def test_editorial_layout_is_deterministic():
    a = _compute_styled(_REFERENCE_TEXT)
    b = _compute_styled(_REFERENCE_TEXT)
    assert a == b


def test_editorial_stagger_monotonic_inside_reveal_window():
    blocks = _compute_styled(_REFERENCE_TEXT, reveal_window_s=3.0)
    starts = [b["start_offset_s"] for b in blocks]
    assert starts == sorted(starts)
    assert starts[0] == 0.0
    for b in blocks:
        assert b["start_offset_s"] < 3.0


# -- third voice: italic serif accent (styled path only) -------------------------


def test_registry_resolves_playfair_italic_face():
    # The accent face must resolve from the registry to the real italic VF —
    # `_typeface_for_overlay` silently falls back to Playfair Bold (NOT italic)
    # for unknown names, so isItalic doubles as a registry-resolution check.
    tf = _typeface_for_overlay({"font_family": "Playfair Display Italic"})
    assert tf.getFamilyName() == "Playfair Display"
    assert tf.isItalic()


def test_editorial_non_hero_blocks_alternate_body_then_accent():
    # _REFERENCE_TEXT groups as connector("what’s your") / hero / closer.
    # Non-hero block k=0 → body, k=1 → italic accent (default accent_parity=0).
    blocks = _compute_styled(_REFERENCE_TEXT)
    assert blocks is not None
    assert [b["font_family"] for b in blocks] == [
        "Playfair Display Regular",
        "Great Vibes",
        "Playfair Display Italic",
    ]
    # Deterministic: identical call → identical faces and geometry.
    assert blocks == _compute_styled(_REFERENCE_TEXT)


def test_editorial_accent_parity_shifts_which_block_is_italic():
    # Reference behavior: consecutive scenes alternate which block opens
    # italic ('if'(italic) 'you'(serif) / 'and'(serif) 'good timing'(italic))
    # — callers pass scene_index as accent_parity.
    even = _compute_styled(_REFERENCE_TEXT, accent_parity=0)
    odd = _compute_styled(_REFERENCE_TEXT, accent_parity=1)
    assert [b["font_family"] for b in even if b["role"] != ROLE_HERO] == [
        "Playfair Display Regular",
        "Playfair Display Italic",
    ]
    assert [b["font_family"] for b in odd if b["role"] != ROLE_HERO] == [
        "Playfair Display Italic",
        "Playfair Display Regular",
    ]
    # Parity is mod-2: scene 2 is byte-identical to scene 0.
    assert _compute_styled(_REFERENCE_TEXT, accent_parity=2) == even


def test_editorial_hero_never_takes_accent_face():
    cases = [
        (_REFERENCE_TEXT, None),
        ("dream big tonight", ["hero", "connector", "hero"]),
        ("the days we lost", ["connector", "hero", "hero", "hero"]),
        ("Rome.", None),  # promoted single-block hero
    ]
    for parity in (0, 1):
        for text, roles in cases:
            blocks = _compute_styled(text, word_roles=roles, accent_parity=parity)
            assert blocks, f"styled engine declined {text!r}"
            for b in blocks:
                if b["role"] == ROLE_HERO:
                    assert b["font_family"] != "Playfair Display Italic", (text, parity)


def test_editorial_accent_glyph_gate_falls_back_to_body_font():
    # Real code path, no mocks: point the accent at Great Vibes (missing 'Δ')
    # and pin 'Δ' into the odd-k non-hero slot. The block must fall back to
    # the body face — and measurement must use that FINAL face (no-clip).
    style = {**EDITORIAL_STYLE, "accent_font": "Great Vibes"}
    blocks = _compute_styled("dream big Δ", word_roles=["hero", "connector", "closer"], style=style)
    assert blocks is not None
    assert [b["role"] for b in blocks] == [ROLE_HERO, ROLE_CONNECTOR, ROLE_CLOSER]
    closer = blocks[2]
    assert "Δ" in closer["text"]
    assert closer["font_family"] == "Playfair Display Regular"
    _assert_no_clip(blocks)
    # Control: with full glyph coverage the same slot takes the accent face.
    control = _compute_styled(
        "dream big now", word_roles=["hero", "connector", "closer"], style=style
    )
    assert control is not None
    assert control[2]["font_family"] == "Great Vibes"


def test_editorial_accent_face_missing_from_registry_falls_back_to_body():
    # A registry-missing accent counts as missing glyphs — the resolver would
    # silently hand back Playfair Bold and the gate would test the wrong face.
    style = {**EDITORIAL_STYLE, "accent_font": "No Such Face 42"}
    blocks = _compute_styled(_REFERENCE_TEXT, style=style)
    assert blocks is not None
    for b in blocks:
        if b["role"] != ROLE_HERO:
            assert b["font_family"] == "Playfair Display Regular"


def test_legacy_path_ignores_accent_parity_and_never_uses_italic():
    # style=None is the kill-switch contract — accent_parity must be inert.
    base = _compute(_REFERENCE_TEXT, font_family="Playfair Display")
    for parity in (0, 1, 5):
        blocks = compute_cluster_blocks(
            _REFERENCE_TEXT,
            word_roles=None,
            base_size_px=60,
            font_family="Playfair Display",
            reveal_window_s=3.0,
            style=None,
            accent_parity=parity,
        )
        assert blocks == base
    assert all(b["font_family"] != "Playfair Display Italic" for b in base)
