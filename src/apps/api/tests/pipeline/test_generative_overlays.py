"""Tests for the generative-edit hero-intro overlay assembly + injection.

The renderer-parity assertion (emitted dict matches the Skia overlay schema) is
mandatory per CLAUDE.md's #296-class history.
"""

from __future__ import annotations

from app.pipeline.generative_overlays import (
    _HOLD_TO_END_S,
    build_intro_overlay,
    build_persistent_intro_overlays,
    inject_intro_overlay,
    inject_persistent_intro,
)


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


# -- build_persistent_intro_overlays: the recipe-free [reveal, hold] builder --
# Lane D burns these directly onto the talking_head composite (no recipe slot), so
# the list builder must produce exactly what inject_persistent_intro injects.


def test_build_persistent_overlays_returns_reveal_and_hold():
    overlays = build_persistent_intro_overlays(
        text="i did not expect this",
        effect="karaoke-line",
        reveal_window_s=3.0,
        position="center",
    )
    assert len(overlays) == 2
    reveal, hold = overlays
    assert reveal["effect"] == "karaoke-line"
    assert reveal["start_s"] == 0.0
    assert reveal["end_s"] == 3.0
    assert hold["effect"] == "static"
    assert hold["start_s"] == 3.0
    assert hold["end_s"] == _HOLD_TO_END_S
    # Same screen slot, back-to-back, both no-merge.
    assert reveal["text"] == hold["text"] == "i did not expect this"
    assert reveal["position"] == hold["position"] == "center"
    assert reveal["role"] == hold["role"] == "generative_intro"


def test_build_persistent_overlays_matches_injected_recipe():
    # The list builder and the recipe injector must stay byte-identical (one wraps the
    # other) — guards the Lane D refactor against drift between the two paths.
    kwargs = dict(text="hello world", effect="pop-in", reveal_window_s=2.5, position="top")
    standalone = build_persistent_intro_overlays(**kwargs)
    injected = inject_persistent_intro({"slots": [{"position": 0}]}, 0, **kwargs)
    assert injected["slots"][0]["text_overlays"] == standalone


def test_build_persistent_overlays_empty_text_returns_empty_list():
    assert build_persistent_intro_overlays(text="  ", effect="static", reveal_window_s=3.0) == []


# -- inject_persistent_intro: reveal then hold for the whole video --


def _hero_overlays(recipe: dict) -> list[dict]:
    return recipe["slots"][0]["text_overlays"]


def test_persistent_intro_emits_reveal_plus_spanning_hold():
    recipe = {"slots": [{"position": 0, "target_duration_s": 5.0}]}
    out = inject_persistent_intro(
        recipe,
        0,
        text="i did not expect this",
        effect="karaoke-line",
        reveal_window_s=3.0,
        position="center",
    )
    overlays = _hero_overlays(out)
    assert len(overlays) == 2
    reveal, hold = overlays
    # Bounded animated reveal over the reveal window (stays under the Skia frame cap).
    assert reveal["effect"] == "karaoke-line"
    assert reveal["start_s"] == 0.0
    assert reveal["end_s"] == 3.0
    total_reveal = sum(w["duration_cs"] for w in reveal["word_timings"]) / 100.0
    assert abs(total_reveal - 3.0) < 0.05
    # Static hold takes over at the reveal end and spans to (past) EOF.
    assert hold["effect"] == "static"
    assert hold["start_s"] == 3.0
    assert hold["end_s"] == _HOLD_TO_END_S
    assert "word_timings" not in hold
    # Same text + position so they sit in the same screen slot, back-to-back.
    assert reveal["text"] == hold["text"] == "i did not expect this"
    assert reveal["position"] == hold["position"] == "center"
    # role marks both no-merge for the Dedup-1 pass.
    assert reveal["role"] == hold["role"] == "generative_intro"


def test_persistent_intro_karaoke_hold_uses_highlight_color():
    # Karaoke settles every word to highlight_color, so the static hold must render in
    # highlight_color to continue seamlessly from the reveal's settled state.
    out = inject_persistent_intro(
        {"slots": [{"position": 0, "target_duration_s": 5.0}]},
        0,
        text="hello world",
        effect="karaoke-line",
        reveal_window_s=3.0,
        text_color="#FFFFFF",
        highlight_color="#FFD24A",
    )
    reveal, hold = _hero_overlays(out)
    assert reveal["text_color"] == "#FFFFFF"
    assert hold["text_color"] == "#FFD24A"  # settled karaoke color


def test_persistent_intro_non_karaoke_hold_uses_text_color():
    # pop-in (and other block effects) settle on text_color, so the hold matches it.
    out = inject_persistent_intro(
        {"slots": [{"position": 0, "target_duration_s": 5.0}]},
        0,
        text="hello world",
        effect="pop-in",
        reveal_window_s=3.0,
        text_color="#FFFFFF",
        highlight_color="#FFD24A",
    )
    reveal, hold = _hero_overlays(out)
    assert reveal["effect"] == "pop-in"
    assert hold["effect"] == "static"
    assert hold["text_color"] == "#FFFFFF"  # settled = text_color for non-karaoke


def test_persistent_intro_style_fields_on_both_overlays():
    out = inject_persistent_intro(
        {"slots": [{"position": 0, "target_duration_s": 5.0}]},
        0,
        text="ai answer",
        effect="stream-in",
        reveal_window_s=3.0,
        font_family="Space Mono",
        text_size_px=56,
        position_x_frac=0.06,
    )
    for ov in _hero_overlays(out):
        assert ov["font_family"] == "Space Mono"
        assert ov["text_size_px"] == 56
        assert ov["position_x_frac"] == 0.06


def test_persistent_intro_empty_text_noop():
    recipe = {"slots": [{"position": 0, "target_duration_s": 5.0}]}
    out = inject_persistent_intro(recipe, 0, text="   ", effect="karaoke-line", reveal_window_s=3.0)
    assert out["slots"][0].get("text_overlays") in (None, [])


def test_persistent_intro_no_slots_noop():
    recipe = {"slots": []}
    out = inject_persistent_intro(recipe, 0, text="hi", effect="static", reveal_window_s=3.0)
    assert out == {"slots": []}


def test_persistent_intro_hero_out_of_range_noop():
    recipe = {"slots": [{"position": 0, "target_duration_s": 5.0}]}
    out = inject_persistent_intro(recipe, 5, text="hi", effect="static", reveal_window_s=3.0)
    assert out["slots"][0].get("text_overlays") in (None, [])


def test_persistent_intro_only_hero_slot_gets_overlays():
    # Injection targets the hero slot ONLY — the static hold spans the whole video via
    # its end_s (overlays burn on the joined video, not per segment), so later slots
    # stay empty.
    recipe = {
        "slots": [
            {"position": 0, "target_duration_s": 5.0},
            {"position": 1, "target_duration_s": 4.0},
        ]
    }
    out = inject_persistent_intro(
        recipe, 0, text="stays up", effect="karaoke-line", reveal_window_s=3.0
    )
    assert len(out["slots"][0]["text_overlays"]) == 2
    assert out["slots"][1].get("text_overlays") in (None, [])


def test_persistent_intro_survives_collect_absolute_overlays():
    # End-to-end against the REAL burn-time overlay collection: the reveal and hold must
    # NOT merge (Dedup 1), the hold must span past every later cut, and the karaoke
    # reveal must keep its word_timings.
    from app.pipeline.agents.gemini_analyzer import AssemblyStep
    from app.tasks.template_orchestrate import _collect_absolute_overlays

    slots = [
        {"position": i + 1, "target_duration_s": d, "priority": 5, "slot_type": "broll"}
        for i, d in enumerate((5.0, 4.0, 6.0))
    ]
    inject_persistent_intro(
        {"slots": slots}, 0, text="i did not expect", effect="karaoke-line", reveal_window_s=3.0
    )
    steps = [AssemblyStep(slot=s, clip_id=f"c{i}", moment={}) for i, s in enumerate(slots)]
    slot_durs = [s["target_duration_s"] for s in slots]
    out = _collect_absolute_overlays(steps, slot_durs, clip_metas=None, subject="", is_agentic=True)

    intros = [o for o in out if o["text"] == "i did not expect"]
    assert len(intros) == 2  # reveal + hold survived as distinct overlays (no merge)
    intros.sort(key=lambda o: o["start_s"])
    reveal, hold = intros
    assert reveal["effect"] == "karaoke-line"
    assert reveal["start_s"] == 0.0 and reveal["end_s"] == 3.0
    assert reveal.get("word_timings")
    assert hold["effect"] == "static"
    assert hold["start_s"] == 3.0
    # Hold spans well past the total video duration (5+4+6 = 15s) — held to EOF.
    assert hold["end_s"] >= sum(slot_durs)
    # Internal bookkeeping is stripped before return.
    assert "_no_merge" not in hold
