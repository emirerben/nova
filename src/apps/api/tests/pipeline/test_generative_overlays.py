"""Tests for the generative-edit hero-intro overlay assembly + injection.

The renderer-parity assertion (emitted dict matches the Skia overlay schema) is
mandatory per CLAUDE.md's #296-class history.
"""

from __future__ import annotations

from app.pipeline.generative_overlays import build_intro_overlay, inject_intro_overlay


def test_karaoke_overlay_has_word_timings_matching_skia_schema():
    ov = build_intro_overlay(
        "i did not expect this",
        effect="karaoke-line",
        size_class="jumbo",
        position="center",
        text_color="#FFFFFF",
        highlight_color="#FFD24A",
        text_anchor="center",
        start_s=0.0,
        end_s=2.5,
    )
    assert ov is not None
    # Fields the Skia renderer reads (text_overlay_skia._draw_karaoke_line +
    # _resolve_font_size_px / _resolve_anchor / _resolve_text_anchor).
    for key in (
        "text",
        "effect",
        "start_s",
        "end_s",
        "position",
        "text_size",
        "text_anchor",
        "text_color",
        "highlight_color",
        "word_timings",
    ):
        assert key in ov, f"missing {key}"
    assert ov["effect"] == "karaoke-line"
    assert ov["subject_substitute"] is False
    wt = ov["word_timings"]
    assert [w["text"] for w in wt] == ["i", "did", "not", "expect", "this"]
    assert all("duration_cs" in w for w in wt)


def test_empty_text_returns_none():
    assert build_intro_overlay("   ", effect="karaoke-line", start_s=0.0, end_s=2.0) is None


def test_non_positive_window_returns_none():
    assert build_intro_overlay("hi there", effect="static", start_s=1.0, end_s=1.0) is None


def test_unknown_effect_coerced_to_static():
    ov = build_intro_overlay("hello world", effect="explode", start_s=0.0, end_s=2.0)
    assert ov is not None
    assert ov["effect"] == "static"
    assert "word_timings" not in ov  # static has no per-word reveal


def test_unknown_position_size_anchor_coerced():
    ov = build_intro_overlay(
        "hello",
        effect="static",
        position="diagonal",
        size_class="ginormous",
        text_anchor="middle",
        start_s=0.0,
        end_s=2.0,
    )
    assert ov["position"] == "center"
    assert ov["text_size"] == "jumbo"
    assert ov["text_anchor"] == "center"


def test_bad_hex_colors_coerced_to_defaults():
    # build_intro_overlay self-defends: a caller passing junk colors can't reach Skia.
    ov = build_intro_overlay(
        "hello",
        effect="static",
        start_s=0.0,
        end_s=2.0,
        text_color="javascript:alert(1)",
        highlight_color="#ZZZ",
    )
    assert ov["text_color"] == "#FFFFFF"
    assert ov["highlight_color"] == "#FFD24A"


def test_valid_hex_color_uppercased():
    ov = build_intro_overlay(
        "hello",
        effect="static",
        start_s=0.0,
        end_s=2.0,
        text_color="#abcdef",
    )
    assert ov["text_color"] == "#ABCDEF"


def test_non_karaoke_effect_has_no_word_timings():
    ov = build_intro_overlay("pop this", effect="pop-in", start_s=0.0, end_s=2.0)
    assert ov["effect"] == "pop-in"
    assert "word_timings" not in ov


def test_beats_passed_through_to_word_timings():
    ov = build_intro_overlay(
        "a b c d", effect="karaoke-line", start_s=0.0, end_s=4.0, beats=[1.0, 2.0, 3.0]
    )
    ends = []
    acc = 0.0
    for w in ov["word_timings"]:
        acc += w["duration_cs"] / 100.0
        ends.append(acc)
    assert all(ends[i] > ends[i - 1] for i in range(1, len(ends)))


def test_highlight_word_stored_as_metadata():
    ov = build_intro_overlay(
        "i did not expect",
        effect="karaoke-line",
        start_s=0.0,
        end_s=2.0,
        highlight_word="not",
    )
    assert ov["highlight_word"] == "not"


def test_style_set_passthrough_fields_on_overlay():
    # Caller (._inject_agent_intro) resolves a style set and passes the concrete
    # fields; build_intro_overlay must surface them in the Skia overlay dict.
    ov = build_intro_overlay(
        "hello world",
        effect="typewriter",
        start_s=0.0,
        end_s=2.0,
        font_family="Space Mono",
        stroke_width=0,
        text_size_px=56,
        position_x_frac=0.06,
    )
    assert ov["effect"] == "typewriter"  # widened allowlist keeps the set's effect
    assert ov["font_family"] == "Space Mono"
    assert ov["stroke_width"] == 0
    assert ov["text_size_px"] == 56
    assert ov["position_x_frac"] == 0.06
    # text_size_px is authoritative — the size_class bucket is dropped so it can't
    # fight the px value at the renderer.
    assert "text_size" not in ov


def test_style_set_effect_survives_when_renderer_known():
    # stream-in is a curated-set effect the Skia renderer draws; it must NOT be
    # flattened to static the way a genuinely unknown effect is.
    ov = build_intro_overlay("ai answer", effect="stream-in", start_s=0.0, end_s=2.0)
    assert ov["effect"] == "stream-in"


def test_no_style_fields_omits_keys():
    # Without style-set passthrough the overlay stays exactly as before (size bucket,
    # no font_family) — back-compat for the no-set / legacy path.
    ov = build_intro_overlay("hi", effect="pop-in", start_s=0.0, end_s=2.0)
    assert ov["text_size"] == "jumbo"
    assert "font_family" not in ov
    assert "text_size_px" not in ov


def test_inject_into_hero_slot():
    recipe = {"slots": [{"position": 0}, {"position": 1, "text_overlays": []}]}
    ov = build_intro_overlay("hi", effect="static", start_s=0.0, end_s=1.0)
    out = inject_intro_overlay(recipe, 1, ov)
    assert len(out["slots"][1]["text_overlays"]) == 1
    assert out["slots"][0].get("text_overlays") in (None, [])


def test_inject_creates_overlay_list_when_missing():
    recipe = {"slots": [{"position": 0}]}
    ov = build_intro_overlay("hi", effect="static", start_s=0.0, end_s=1.0)
    out = inject_intro_overlay(recipe, 0, ov)
    assert out["slots"][0]["text_overlays"][0]["text"] == "hi"


def test_inject_hero_out_of_range_is_noop():
    recipe = {"slots": [{"position": 0}]}
    ov = build_intro_overlay("hi", effect="static", start_s=0.0, end_s=1.0)
    out = inject_intro_overlay(recipe, 5, ov)
    assert "text_overlays" not in out["slots"][0]


def test_inject_none_overlay_is_noop():
    recipe = {"slots": [{"position": 0}]}
    out = inject_intro_overlay(recipe, 0, None)
    assert out["slots"][0].get("text_overlays") in (None, [])


def test_inject_no_slots_is_noop():
    recipe = {"slots": []}
    ov = build_intro_overlay("hi", effect="static", start_s=0.0, end_s=1.0)
    assert inject_intro_overlay(recipe, 0, ov) == {"slots": []}
