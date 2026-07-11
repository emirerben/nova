"""Tests for the generative-edit hero-intro overlay assembly + injection.

The renderer-parity assertion (emitted dict matches the Skia overlay schema) is
mandatory per CLAUDE.md's #296-class history.
"""

from __future__ import annotations

from app.pipeline.generative_overlays import (
    _HOLD_TO_END_S,
    HOOK_WINDOW_S,
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
    # Use reveal_window_s < _HOLD_TO_END_S so a hold overlay is produced.
    overlays = build_persistent_intro_overlays(
        text="i did not expect this",
        effect="karaoke-line",
        reveal_window_s=2.0,
        position="center",
    )
    assert len(overlays) == 2
    reveal, hold = overlays
    assert reveal["effect"] == "karaoke-line"
    assert reveal["start_s"] == 0.0
    assert reveal["end_s"] == 2.0
    assert hold["effect"] == "static"
    assert hold["start_s"] == 2.0
    assert hold["end_s"] == _HOLD_TO_END_S  # default holds to EOF (matches browser preview)
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
    # Pass hook_window_s=_HOLD_TO_END_S to test the overlay structure independent of
    # the default cap — the hold must span past EOF when the caller opts into it.
    recipe = {"slots": [{"position": 0, "target_duration_s": 5.0}]}
    out = inject_persistent_intro(
        recipe,
        0,
        text="i did not expect this",
        effect="karaoke-line",
        reveal_window_s=3.0,
        position="center",
        hook_window_s=_HOLD_TO_END_S,
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
        reveal_window_s=2.0,
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
        reveal_window_s=2.0,
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
        reveal_window_s=2.0,
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
        recipe, 0, text="stays up", effect="karaoke-line", reveal_window_s=2.0
    )
    assert len(out["slots"][0]["text_overlays"]) == 2
    assert out["slots"][1].get("text_overlays") in (None, [])


def test_persistent_intro_survives_collect_absolute_overlays():
    # End-to-end against the REAL burn-time overlay collection: the reveal and hold must
    # NOT merge (Dedup 1), the hold must span past every later cut, and the karaoke
    # reveal must keep its word_timings. Explicit hook_window_s=_HOLD_TO_END_S to test
    # the collect path independent of the default cap.
    from app.pipeline.agents.gemini_analyzer import AssemblyStep
    from app.tasks.template_orchestrate import _collect_absolute_overlays

    slots = [
        {"position": i + 1, "target_duration_s": d, "priority": 5, "slot_type": "broll"}
        for i, d in enumerate((5.0, 4.0, 6.0))
    ]
    inject_persistent_intro(
        {"slots": slots},
        0,
        text="i did not expect",
        effect="karaoke-line",
        reveal_window_s=3.0,
        hook_window_s=_HOLD_TO_END_S,
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


# -- Word-cluster layout (layout="cluster") -----------------------------------


def _cluster_overlays(**kw):
    defaults = dict(
        text="what's your favorite place?",
        effect="fade-in",
        reveal_window_s=3.0,
        layout="cluster",
        text_size_px=60,
        # Use legacy end-of-file cap so existing cluster tests verify overlay structure
        # independent of the hook-window default.
        hook_window_s=_HOLD_TO_END_S,
    )
    defaults.update(kw)
    return build_persistent_intro_overlays(**defaults)


def test_cluster_emits_reveal_hold_pair_per_block():
    overlays = _cluster_overlays()
    assert len(overlays) >= 4 and len(overlays) % 2 == 0
    reveals = [o for o in overlays if o["effect"] == "fade-in"]
    holds = [o for o in overlays if o["effect"] == "static"]
    assert len(reveals) == len(holds)
    for r, h in zip(reveals, holds, strict=True):
        assert r["text"] == h["text"]
        assert r["position_x_frac"] == h["position_x_frac"]
        assert r["position_y_frac"] == h["position_y_frac"]
        assert r["text_size_px"] == h["text_size_px"]
        assert h["start_s"] == r["end_s"]
        assert h["end_s"] == _HOLD_TO_END_S
        # No-merge protection (Dedup 1) hinges on this role.
        assert r["role"] == h["role"] == "generative_intro"
        assert r["subject_substitute"] is False
    # Staggered reveal: not every block starts at 0.
    starts = sorted(r["start_s"] for r in reveals)
    assert starts[0] == 0.0 and starts[-1] > 0.0


def test_cluster_blocks_cover_all_words():
    overlays = _cluster_overlays()
    reveals = [o for o in overlays if o["effect"] == "fade-in"]
    assert " ".join(o["text"] for o in reveals) == "what's your favorite place?"


def test_cluster_blocks_have_mixed_sizes_and_positions():
    overlays = _cluster_overlays()
    reveals = [o for o in overlays if o["effect"] == "fade-in"]
    sizes = {o["text_size_px"] for o in reveals}
    positions = {(o["position_x_frac"], o["position_y_frac"]) for o in reveals}
    assert len(sizes) >= 2  # the whole point: NOT one uniform block
    assert len(positions) == len(reveals)


def test_cluster_engine_failure_falls_back_to_linear(monkeypatch):
    import app.pipeline.intro_cluster as ic

    def boom(*a, **kw):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(ic, "compute_cluster_blocks", boom)
    overlays = _cluster_overlays()  # uses hook_window_s=_HOLD_TO_END_S
    linear = build_persistent_intro_overlays(
        text="what's your favorite place?",
        effect="fade-in",
        reveal_window_s=3.0,
        layout="linear",
        text_size_px=60,
        hook_window_s=_HOLD_TO_END_S,
    )
    assert overlays == linear  # exact linear pair — fallback parity


def test_cluster_unsuitable_text_falls_back_to_linear():
    # 2 words < MIN_WORDS → engine declines → linear [reveal, hold].
    overlays = _cluster_overlays(text="hello world")
    assert len(overlays) == 2
    assert overlays[0]["text"] == "hello world"


def test_linear_layout_output_unchanged_by_cluster_kwargs():
    # layout="linear" + word_roles must be byte-identical to the legacy call.
    legacy = build_persistent_intro_overlays(
        text="i did not expect", effect="karaoke-line", reveal_window_s=3.0
    )
    with_kwargs = build_persistent_intro_overlays(
        text="i did not expect",
        effect="karaoke-line",
        reveal_window_s=3.0,
        layout="linear",
        word_roles=["hero", "connector", "connector", "hero"],
    )
    assert legacy == with_kwargs


def test_inject_persistent_intro_forwards_cluster_layout():
    recipe = {"slots": [{"position": 1, "target_duration_s": 5.0, "text_overlays": []}]}
    out = inject_persistent_intro(
        recipe,
        0,
        text="what's your favorite place?",
        effect="fade-in",
        reveal_window_s=3.0,
        layout="cluster",
        text_size_px=60,
        hook_window_s=_HOLD_TO_END_S,
    )
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) >= 4  # multi-block cluster, not the linear pair


def test_cluster_reveal_windows_are_frame_budget_bounded():
    # Perf lock: each animated reveal is a per-frame PNG sequence at 30fps. A
    # bounded ~0.7s window (~22 frames/block) keeps a 5-block cluster cheaper
    # than one full-length karaoke intro (~91 frames). The holds are static
    # (1 looped PNG each) so only the fade-in windows matter.
    overlays = _cluster_overlays(text="the days we never planned", reveal_window_s=3.0)
    reveals = [o for o in overlays if o["effect"] == "fade-in"]
    assert reveals
    total_animated_s = 0.0
    for o in reveals:
        window = o["end_s"] - o["start_s"]
        assert window <= 0.71, f"unbounded reveal window on {o['text']!r}"
        total_animated_s += window
    assert total_animated_s * 30 <= 150  # ≤ ~150 animated frames per cluster


# ── Transcript-synced typographic sequence (build_sequence_overlays) ─────────────
# The cluster engine is stubbed (geometry has its own tests in
# test_intro_cluster.py); these tests pin the reference-verified emission:
# ONE static overlay per block (instant pop-in, no reveal/hold pair),
# beat-spread offsets that IGNORE the engine's timing, hard cuts on normal
# scenes, the 350ms tail + oldest-first dismantle on fade_out scenes, scene
# y-alternation, and the skip/None fallback semantics.


def _seq_scenes() -> list[dict]:
    return [
        {
            "words": ["when", "the", "days"],
            "word_roles": ["connector", "connector", "hero"],
            "speech_start_s": 0.4,
            "speech_end_s": 1.9,
            "start_s": 0.0,
            "end_s": 2.25,
            "fade_out": False,
        },
        {
            "words": ["found", "us."],
            "word_roles": ["hero", "closer"],
            "speech_start_s": 2.0,
            "speech_end_s": 2.9,
            "start_s": 2.0,
            "end_s": 6.0,
            "fade_out": True,
        },
    ]


def _patch_seq_engine(monkeypatch, *, decline_indices=(), blocks_per_scene=1):
    """Stub compute_cluster_blocks with `blocks_per_scene` deterministic blocks.
    The blocks carry NONSENSE engine timing (start_offset_s/reveal_s) to prove
    the emitter ignores it. Returns the capture list of calls."""
    import app.pipeline.intro_cluster as ic

    calls: list[dict] = []
    counter = {"i": 0}

    def _fake_blocks(
        text,
        *,
        word_roles,
        base_size_px,
        font_family=None,
        reveal_window_s,
        style,
        accent_parity=0,
    ):
        idx = counter["i"]
        counter["i"] += 1
        calls.append(
            {
                "index": idx,
                "text": text,
                "word_roles": word_roles,
                "base_size_px": base_size_px,
                "reveal_window_s": reveal_window_s,
                "style": style,
                "accent_parity": accent_parity,
            }
        )
        if idx in decline_indices:
            return None
        return [
            {
                "text": f"{text}#{b}",
                "role": "hero",
                "text_size_px": base_size_px,
                "font_family": "Great Vibes",
                "position_x_frac": 0.4,
                "position_y_frac": 0.44,
                "start_offset_s": 9.9,  # must be ignored by the emitter
                "reveal_s": 9.9,  # must be ignored by the emitter
            }
            for b in range(blocks_per_scene)
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)
    return calls


def test_sequence_emits_one_static_overlay_per_block(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays
    from app.pipeline.intro_cluster import EDITORIAL_STYLE

    calls = _patch_seq_engine(monkeypatch)
    overlays = build_sequence_overlays(_seq_scenes(), base_size_px=78)
    assert overlays is not None
    assert len(overlays) == 2  # ONE overlay per scene's single block — no pairs

    # Engine called with the styled profile + the caller-filled roles + the
    # scene length as the (timing-irrelevant) reveal window + the scene index
    # as accent parity.
    assert [c["style"] for c in calls] == [EDITORIAL_STYLE, EDITORIAL_STYLE]
    assert calls[0]["word_roles"] == ["connector", "connector", "hero"]
    assert calls[0]["base_size_px"] == 78
    assert calls[0]["reveal_window_s"] == 2.25  # scene_len, not the speech span
    assert calls[1]["reveal_window_s"] == 4.0
    assert [c["accent_parity"] for c in calls] == [0, 1]

    s0, s1 = overlays
    # Every overlay carries the sequence role (renderer fade machinery keys off it).
    assert all(o["role"] == "generative_sequence" for o in overlays)
    assert all(o["effect"] == "static" for o in overlays)
    # Scene 0 (normal): instant pop at the ABSOLUTE scene start, HARD CUT at
    # end_s — no fade tail.
    assert (s0["start_s"], s0["end_s"]) == (0.0, 2.25)
    assert "fade_out_ms" not in s0
    # Scene 1 (fade_out): pops at its own absolute start, 350ms alpha tail.
    assert (s1["start_s"], s1["end_s"]) == (2.0, 6.0)
    assert s1["fade_out_ms"] == 350


def test_sequence_beat_spread_offsets_ignore_engine_timing(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    # One scene, 3 blocks: beat = clamp((2.25 - 0.6) / 2, 0.35, 0.8) = 0.8.
    _patch_seq_engine(monkeypatch, blocks_per_scene=3)
    overlays = build_sequence_overlays([_seq_scenes()[0]], base_size_px=78)
    assert len(overlays) == 3
    starts = [o["start_s"] for o in overlays]
    # Monotonically increasing, spaced by the clamped beat — NOT the engine's
    # nonsense start_offset_s (9.9).
    assert starts == [0.0, 0.8, 1.6]
    # All blocks hold to the scene end (hard cut), instant static pops.
    assert all(o["end_s"] == 2.25 for o in overlays)
    assert all(o["effect"] == "static" for o in overlays)
    assert all("fade_out_ms" not in o for o in overlays)


def test_sequence_beat_clamped_to_floor_and_min_visibility(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    # scene_len = 1.0, 3 blocks: raw beat (1.0 - 0.6) / 2 = 0.2 → floor 0.35.
    # Offsets 0 / 0.35 / 0.7, but every block must show >= 0.45s, so the last
    # offset clamps to scene_len - 0.45 = 0.55.
    _patch_seq_engine(monkeypatch, blocks_per_scene=3)
    scene = {
        "words": ["so", "much", "fun"],
        "word_roles": None,
        "start_s": 2.0,
        "end_s": 3.0,
        "fade_out": False,
    }
    overlays = build_sequence_overlays([scene], base_size_px=78)
    starts = [o["start_s"] for o in overlays]
    assert starts == [2.0, 2.35, 2.55]
    assert all(o["end_s"] - o["start_s"] >= 0.45 for o in overlays)


def test_sequence_beat_clamped_to_ceiling_on_slow_scene(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    # scene_len = 6.0, 2 blocks: raw beat (6.0 - 0.6) / 1 = 5.4 → ceiling 0.8.
    _patch_seq_engine(monkeypatch, blocks_per_scene=2)
    scene = {
        "words": ["pure", "magic"],
        "word_roles": None,
        "start_s": 0.0,
        "end_s": 6.0,
        "fade_out": False,
    }
    overlays = build_sequence_overlays([scene], base_size_px=78)
    assert [o["start_s"] for o in overlays] == [0.0, 0.8]


def test_sequence_fade_out_scene_dismantles_oldest_block_first(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    # fade_out scene with 3 blocks: ALL carry the 350ms tail and the FIRST
    # block ends 0.2s before the scene (oldest-word-first dismantle).
    _patch_seq_engine(monkeypatch, blocks_per_scene=3)
    scene = dict(_seq_scenes()[1], words=["luck", "is", "timing"], word_roles=None)
    overlays = build_sequence_overlays([scene], base_size_px=78)
    assert len(overlays) == 3
    assert all(o["fade_out_ms"] == 350 for o in overlays)
    first, second, third = overlays
    assert first["end_s"] == 5.8  # 6.0 - 0.2, still > its start
    assert first["end_s"] > first["start_s"]
    assert second["end_s"] == 6.0
    assert third["end_s"] == 6.0


def test_sequence_fade_out_scene_with_two_blocks_keeps_full_window(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    # < 3 blocks → no dismantle: both blocks hold to the scene end.
    _patch_seq_engine(monkeypatch, blocks_per_scene=2)
    overlays = build_sequence_overlays([_seq_scenes()[1]], base_size_px=78)
    assert len(overlays) == 2
    assert all(o["end_s"] == 6.0 for o in overlays)
    assert all(o["fade_out_ms"] == 350 for o in overlays)


def test_sequence_overlays_apply_scene_y_alternation(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays
    from app.pipeline.intro_cluster import _CLUSTER_CENTER_Y, scene_center_y

    _patch_seq_engine(monkeypatch)
    overlays = build_sequence_overlays(_seq_scenes(), base_size_px=78)
    s0, s1 = overlays
    # Blocks were emitted at y=0.44; the whole scene shifts by the deterministic
    # scene-center cycle so consecutive scenes never sit at the identical y.
    assert s0["position_y_frac"] == round(0.44 + scene_center_y(0) - _CLUSTER_CENTER_Y, 6)
    assert s1["position_y_frac"] == round(0.44 + scene_center_y(1) - _CLUSTER_CENTER_Y, 6)
    assert s0["position_y_frac"] != s1["position_y_frac"]


def test_sequence_overlays_carry_block_typography_and_skia_schema(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    _patch_seq_engine(monkeypatch)
    overlays = build_sequence_overlays(_seq_scenes(), base_size_px=78, text_color="#FFEEDD")
    for o in overlays:
        assert o["font_family"] == "Great Vibes"
        assert o["text_size_px"] == 78
        assert o["position_x_frac"] == 0.4
        assert o["text_anchor"] == "center"
        assert o["text_color"] == "#FFEEDD"
        assert o["subject_substitute"] is False
        # px is authoritative — the size bucket must not survive alongside it.
        assert "text_size" not in o


def test_sequence_overlay_without_block_color_is_byte_stable(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    _patch_seq_engine(monkeypatch)
    overlays = build_sequence_overlays([_seq_scenes()[0]], base_size_px=78, text_color="#FFEEDD")
    assert overlays == [
        {
            "role": "generative_sequence",
            "text": "when the days#0",
            "effect": "static",
            "start_s": 0.0,
            "end_s": 2.25,
            "position": "center",
            "text_anchor": "center",
            "text_color": "#FFEEDD",
            "highlight_color": "#FFD24A",
            "subject_substitute": False,
            "font_family": "Great Vibes",
            "text_size_px": 78,
            "position_x_frac": 0.4,
            "position_y_frac": 0.42,
        }
    ]


def test_sequence_overlays_use_block_text_color_and_plumb_glow(monkeypatch):
    import app.pipeline.intro_cluster as ic
    from app.pipeline.generative_overlays import build_sequence_overlays

    def _fake_blocks(*args, **kwargs):
        return [
            {
                "text": "when",
                "role": "connector",
                "text_size_px": 78,
                "font_family": "Playfair Display Regular",
                "position_x_frac": 0.4,
                "position_y_frac": 0.44,
                "start_offset_s": 9.9,
                "reveal_s": 9.9,
            },
            {
                "text": "days",
                "role": "hero",
                "text_size_px": 96,
                "font_family": "Great Vibes",
                "position_x_frac": 0.5,
                "position_y_frac": 0.5,
                "start_offset_s": 9.9,
                "reveal_s": 9.9,
                "text_color": "#D9F65A",
                "glow_color": "#7CFF8A",
                "glow_strength": 0.6,
            },
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)
    overlays = build_sequence_overlays([_seq_scenes()[0]], base_size_px=78, text_color="#FFEEDD")
    assert overlays is not None
    assert overlays[0]["text_color"] == "#FFEEDD"
    assert "glow_color" not in overlays[0]
    assert overlays[1]["text_color"] == "#D9F65A"
    assert overlays[1]["glow_color"] == "#7CFF8A"
    assert overlays[1]["glow_strength"] == 0.6


def test_sequence_overlays_skip_declined_scene(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    _patch_seq_engine(monkeypatch, decline_indices={0})
    overlays = build_sequence_overlays(_seq_scenes(), base_size_px=78)
    assert overlays is not None
    assert len(overlays) == 1  # scene 0 skipped, scene 1 renders
    assert all(o["start_s"] >= 2.0 for o in overlays)


def test_sequence_overlays_all_scenes_declined_returns_none(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    _patch_seq_engine(monkeypatch, decline_indices={0, 1})
    assert build_sequence_overlays(_seq_scenes(), base_size_px=78) is None


def test_sequence_overlays_empty_scenes_returns_none(monkeypatch):
    from app.pipeline.generative_overlays import build_sequence_overlays

    _patch_seq_engine(monkeypatch)
    assert build_sequence_overlays([], base_size_px=78) is None


# ── Static-cluster style threading (the kill-switch contract, D13) ───────────────


def test_cluster_intro_threads_style_to_engine(monkeypatch):
    import app.pipeline.intro_cluster as ic

    sentinel = {"hero_font": "X"}
    seen: list[dict | None] = []

    def _fake_blocks(text, *, style=None, **kwargs):
        seen.append(style)
        return None  # decline → linear fallback; we only assert the pass-through

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)
    build_persistent_intro_overlays(
        text="what a day this was",
        effect="fade-in",
        reveal_window_s=2.0,
        layout="cluster",
        cluster_style=sentinel,
        text_size_px=60,
    )
    assert seen == [sentinel]


def test_cluster_intro_default_style_is_none_legacy(monkeypatch):
    # Kill-switch regression contract: no cluster_style kwarg → the engine gets
    # style=None (byte-identical legacy cluster geometry).
    import app.pipeline.intro_cluster as ic

    seen: list[dict | None] = []

    def _fake_blocks(text, *, style=None, **kwargs):
        seen.append(style)
        return None

    monkeypatch.setattr(ic, "compute_cluster_blocks", _fake_blocks)
    build_persistent_intro_overlays(
        text="what a day this was",
        effect="fade-in",
        reveal_window_s=2.0,
        layout="cluster",
        text_size_px=60,
    )
    assert seen == [None]


# ── hook_window_s — time-boxing the hook text ─────────────────────────────────


def test_hook_window_caps_hold_end_s():
    # When hook_window_s=HOOK_WINDOW_S is passed, the static hold must not extend past it.
    # This guards the HOOK_WINDOW_S constant: if its value changes, this test fails.
    overlays = build_persistent_intro_overlays(
        text="you won't believe this",
        effect="fade-in",
        reveal_window_s=2.0,
        hook_window_s=HOOK_WINDOW_S,
    )
    reveal, hold = overlays
    assert reveal["start_s"] == 0.0
    assert reveal["end_s"] == 2.0
    assert hold["start_s"] == 2.0
    assert hold["end_s"] == HOOK_WINDOW_S  # capped at hook_window_s, not _HOLD_TO_END_S


def test_hook_window_omits_hold_when_reveal_fills_window():
    # When reveal_window_s >= hook_window_s there is nothing left to hold.
    overlays = build_persistent_intro_overlays(
        text="you won't believe this",
        effect="fade-in",
        reveal_window_s=HOOK_WINDOW_S,
        hook_window_s=HOOK_WINDOW_S,
    )
    assert len(overlays) == 1
    assert overlays[0]["effect"] == "fade-in"


def test_hook_window_respected_by_inject():
    recipe = {"slots": [{"position": 0, "target_duration_s": 10.0}]}
    out = inject_persistent_intro(
        recipe,
        0,
        text="catch your eye",
        effect="pop-in",
        reveal_window_s=2.0,
        hook_window_s=HOOK_WINDOW_S,
    )
    reveal, hold = _hero_overlays(out)
    assert hold["end_s"] == HOOK_WINDOW_S


def test_hook_window_default_holds_to_eof():
    # Default behaviour (no hook_window_s): the hold runs to EOF — matching the
    # browser preview which shows the intro text for the full video.
    overlays = build_persistent_intro_overlays(
        text="first seconds matter",
        effect="pop-in",
        reveal_window_s=2.0,
    )
    reveal, hold = overlays
    assert hold["end_s"] == _HOLD_TO_END_S

    # Cluster path also defaults to EOF.
    cluster_overlays = build_persistent_intro_overlays(
        text="what is your story here",
        effect="fade-in",
        reveal_window_s=2.0,
        layout="cluster",
        text_size_px=60,
    )
    holds = [o for o in cluster_overlays if o["effect"] == "static"]
    assert holds, "cluster should produce at least one hold overlay"
    for h in holds:
        assert h["end_s"] == _HOLD_TO_END_S, (
            f"cluster hold end_s {h['end_s']} should be _HOLD_TO_END_S by default"
        )


def test_hook_window_cluster_hold_capped():
    # Cluster path: each block's hold must also respect hook_window_s.
    overlays = build_persistent_intro_overlays(
        text="what is your story here",
        effect="fade-in",
        reveal_window_s=3.0,
        layout="cluster",
        text_size_px=60,
        hook_window_s=3.0,
    )
    holds = [o for o in overlays if o["effect"] == "static"]
    for h in holds:
        assert h["end_s"] <= 3.0, f"hold end_s {h['end_s']} exceeds hook_window_s 3.0"
